"""Durable capture of every rendered proposer input.

The proposer stack decides what the next generation *is*, so its input is worth
reading back. Without a durable capture it is unrecoverable after the call: the
renderers are pure, but the channels they render from (``patterns``, the loss
summary, the prior-experiment digest, the genealogy and calibration blocks, the
retry feedback) are never persisted, so a past round's prompt could only be
re-derived approximately. Board runs, by contrast, keep their full transcript.
This module closes that asymmetry: it writes the rendered system and user text
verbatim to one append-only JSONL per epoch,
``epochs/{epoch_id}/proposer_inputs.jsonl``, one line per LLM call.

The file is a new at-rest location for board-derived content, beside
``brief.md`` (spliced verbatim into the system prompt) and
``mutations.json`` (the manifest the proposer was offered). It exposes
nothing new to the proposer — the proposer already received every byte of
what is written here — so the restricted-visibility envelope
(``docs/design/PROPOSER.md`` §5.8) is unchanged by the capture. Capture is
therefore unconditional: a diagnostic that has to be enabled in advance is
absent from exactly the round that needed it.

Three invariants hold the writer together:

* **Capture happens BEFORE the call.** An attempt that times out or raises
  is the one whose input matters most, and the response path never runs
  for it.
* **The append is one syscall under a process-local lock.** A best-of-N
  slate gathers up to ``propose_parallelism`` slots at once, each writing a
  record of tens of kilobytes. A buffered text-mode write is several
  ``write()`` calls, so a concurrent writer can splice its chunks into the
  middle of another record, which is interior corruption rather than a torn
  tail. The lock
  registry mirrors ``_REPO_WORKTREE_LOCKS`` in
  :mod:`zicato.epoch.git_genstore`: callers happen to serialise on the
  orchestrator's event-loop thread, but that is a property of the caller, so
  the writer owns the exclusion itself. Cross-process exclusion comes from the
  workspace runtime lock (one orchestrator per workspace), which is why no
  ``flock`` is taken; ``O_APPEND`` atomicity assumes a local filesystem.
* **The write never raises.** A failed capture logs at DEBUG and the round
  continues, the posture every additive write takes — an optional record
  must not become load-bearing.

Torn-tail tolerance lives on the read side: the newline is the commit, and
:func:`read_proposer_inputs` skips an unparseable final line. An
unparseable INTERIOR line raises, because under the append-only writer
only the tail can be torn.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import TYPE_CHECKING, Any

from zicato.util.best_effort import best_effort
from zicato.util.iso_time import now_iso

if TYPE_CHECKING:  # pragma: no cover - typing-only import
    from collections.abc import Iterator
    from pathlib import Path

log = logging.getLogger(__name__)

#: The proposal call — the text shim's retry loop and the default ADK
#: agent's per-attempt run alike. Best-of-N slate slots and the ``pi``
#: proposer route through these, so they carry this role too.
ROLE_PROPOSAL = "proposal"
#: The best-of-N self-critique call that picks the winning candidate.
ROLE_CRITIQUE = "critique"
#: The LLM-guided recombination merge that composes a round's pair.
ROLE_RECOMBINE_MERGE = "recombine_merge"

#: Process-local append locks keyed by resolved capture-file path, so a
#: threaded caller cannot interleave two records. See the module docstring.
_APPEND_LOCKS: dict[str, threading.Lock] = {}
_APPEND_LOCKS_GUARD = threading.Lock()


def _append_lock(path: Path) -> threading.Lock:
    """Return the process-wide append lock for one capture-file path."""
    key = str(path.resolve())
    with _APPEND_LOCKS_GUARD:
        return _APPEND_LOCKS.setdefault(key, threading.Lock())


def _append_line(path: Path, line: bytes) -> None:
    """Append one fully-encoded line to ``path`` as a single ``O_APPEND`` write.

    The loop is for a short write (a signal, a full disk): the record must
    land whole or the file carries interior corruption. It runs under the
    caller's lock, so the continuation cannot be interleaved.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        written = 0
        while written < len(line):
            written += os.write(fd, line[written:])
    finally:
        os.close(fd)


def capture_proposer_input(
    *,
    workspace_root: Path | None,
    epoch_id: str,
    role: str,
    system: str,
    user: str,
    model: str = "",
    parent_generation_id: str = "",
    new_generation_id: str = "",
    attempt: int | None = None,
    slot: int | None = None,
) -> None:
    """Record one proposer LLM call's rendered input. Best-effort.

    Call this immediately BEFORE the call it describes. ``system`` and
    ``user`` are the exact strings handed to the model; on the ADK path the
    system half belongs to the agent (it owns its static instruction), so
    that record's ``system`` is empty by design.

    ``workspace_root`` may be the outer project dir or the inner
    ``.zicato/`` — the path helper descends. ``None`` (a standalone propose
    with no workspace on disk) is a no-op, as is any I/O failure.
    """
    if workspace_root is None:
        return
    with best_effort(
        "proposer input capture",
        on_error=lambda exc: log.debug("proposer input capture skipped: %s", exc),
    ):
        from zicato.core.workspace import proposer_inputs_path  # noqa: PLC0415

        record: dict[str, Any] = {
            "ts": now_iso(),
            "role": role,
            "epoch_id": epoch_id,
            "parent_generation_id": parent_generation_id,
            "new_generation_id": new_generation_id,
            "model": model,
            "attempt": attempt,
            "slot": slot,
            "system": system,
            "user": user,
        }
        path = proposer_inputs_path(workspace_root, epoch_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")
        with _append_lock(path):
            _append_line(path, line)


def read_proposer_inputs(workspace_root: Path, epoch_id: str) -> Iterator[dict[str, Any]]:
    """Yield one epoch's captured proposer inputs, oldest call first.

    An absent file yields nothing (no round has proposed yet, or every
    capture degraded). An unparseable FINAL line is skipped — a crash
    mid-append costs only the unfinished record. An unparseable interior
    line raises :class:`ValueError`: under the append-only writer only the
    tail can be torn, so interior corruption means something bypassed the
    writer and must surface rather than silently dropping a call.
    """
    from zicato.core.workspace import proposer_inputs_path  # noqa: PLC0415

    path = proposer_inputs_path(workspace_root, epoch_id)
    if not path.exists():
        return
    raw = path.read_text(encoding="utf-8").splitlines()
    lines = [(i, text) for i, text in enumerate(raw) if text.strip()]
    for pos, (line_no, text) in enumerate(lines):
        try:
            record = json.loads(text)
        except json.JSONDecodeError:
            if pos == len(lines) - 1:
                continue  # torn tail — the newline is the commit
            raise ValueError(
                f"proposer input capture {path} line {line_no + 1} is corrupt "
                "(not the tail — the append-only invariant was violated)"
            ) from None
        if isinstance(record, dict):
            yield record


__all__ = [
    "ROLE_CRITIQUE",
    "ROLE_PROPOSAL",
    "ROLE_RECOMBINE_MERGE",
    "capture_proposer_input",
    "read_proposer_inputs",
]
