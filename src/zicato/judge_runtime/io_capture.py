"""Judge-I/O capture — the verbatim sidecar board reflection adjudicates.

An inline judge's verdict survives a run only as a `JudgementEmitted`
event with a one-line ``detail``; the judge's exact INPUT (the reasoning
text it graded) and the raw LLM response it parsed were dropped on the
floor (BOARD-REFLECTION.md's capture gap). This module is the seam that
retains them: a tiny sink protocol
(:class:`JudgeIOSink`), a best-effort append-only file sink
(:class:`JudgeIOFileSink`) writing one JSON line per judge ``evaluate``
call to a **zicato-owned** ``judge_io.jsonl`` beside the run's
``loss.json`` (``judge_io.r{n}.jsonl`` per replicate — the sidecar is
NOT a new ``events.jsonl`` frame; goldfive's proto taxonomy
is pinned by three parsers), and a tolerant reader
(:func:`read_judge_io`).

Record shape (one line per judge ``evaluate`` call that reached the LLM)::

    { "format_version": 1, "judge_name": ..., "ts": ..., "call_index": ...,
      "input": { "reasoning_text": ..., "reasoning_sha256": ...,
                 "transcript_window": [...], "clipped": bool },
      "raw_response": ...,
      "verdict": { "drift_emitted": bool, "kind": ..., "severity": ...,
                   "detail": ... } }

One record per call that reached the LLM, plus one per call that RAISED
before it could: those carry ``verdict.kind ==``
:data:`JUDGE_IO_ERROR_KIND` and the exception text in ``verdict.detail``
(issue #121 — a failed call is not a missed fire).

Text fields clip at :data:`JUDGE_IO_CLIP_CHARS`; ``reasoning_sha256`` is
the sha256 of the **UNCLIPPED** reasoning text, so an adjudicator can
prove it is reading the exact bytes the judge read even when the stored
copy was truncated.

Capture is BEST-EFFORT by contract: every sink failure is logged and
swallowed — a capture problem must never change a verdict, re-score a
run, or abort anything. With no sink wired (``RuntimeConfig.judge_io_sink
is None`` — the ``persist_judge_io=False`` path, and every caller that
predates the seam) the judge path is byte-identical to before this
module existed.

Scope note: capture rides :class:`_InlineCriterionJudge` (LLM-as-a-judge)
only. ``python``-mode judges (:class:`_PythonJudgeWrapper`) are
operator-owned code with no zicato-visible LLM call — there is no "raw
response" to retain — so they are inline-only. Their verdicts land as
``JudgementEmitted`` events like any other judge's.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger("zicato.judge_runtime.io_capture")

#: ``format_version`` stamped onto every ``judge_io.jsonl`` line. The reader
#: accepts exactly this version per line and skips anything else — a garbage
#: or future-format line degrades to "not captured", never a crash.
JUDGE_IO_FORMAT_VERSION: int = 1

#: Per-field clip (64 KiB) for the verbatim text fields (reasoning text,
#: each transcript-window turn, the raw response). ``reasoning_sha256`` is
#: computed over the UNCLIPPED reasoning text before the clip applies.
JUDGE_IO_CLIP_CHARS: int = 65536

#: Marker appended to every clipped text field in ``judge_io.jsonl``.
JUDGE_IO_CLIP_MARKER: str = " … [truncated]"

#: ``verdict.kind`` on a record for a call that RAISED instead of returning a
#: verdict. Such a record carries ``drift_emitted=False`` (the judge did not
#: fire — it did not answer at all), an empty ``raw_response`` (there was no
#: response to parse), and ``detail = "<ExceptionType>: <message>"``. It is
#: what lets board reflection tell a broken judge endpoint apart from a
#: criterion that is simply too narrow: both leave the same silence in
#: ``events.jsonl``, and only one of them is a board-design problem.
JUDGE_IO_ERROR_KIND: str = "error"


def judge_io_path_for_loss(loss_path: Path) -> Path:
    """Map ONE board unit's ``loss.json`` path to its ``judge_io.jsonl`` twin.

    Pure sibling-name math mirroring
    :func:`zicato.tournament.unit_cache.unit_result_path`:
    ``loss.json`` → ``judge_io.jsonl`` and ``loss.r{n}.json`` →
    ``judge_io.r{n}.jsonl``, so the sidecar rides the same replicate slot
    as the loss it accompanies.
    """
    name = loss_path.name
    if name.startswith("loss.") and name.endswith(".json"):
        middle = name[len("loss.") : -len(".json")]  # "" for loss.json, "r3" for loss.r3.json
        if middle:
            return loss_path.with_name(f"judge_io.{middle}.jsonl")
    return loss_path.with_name("judge_io.jsonl")


def _clip(text: str) -> tuple[str, bool]:
    """Clip one text field; return ``(text, was_clipped)``."""
    if len(text) <= JUDGE_IO_CLIP_CHARS:
        return text, False
    return text[:JUDGE_IO_CLIP_CHARS] + JUDGE_IO_CLIP_MARKER, True


def build_judge_io_record(
    *,
    judge_name: str,
    call_index: int,
    reasoning_text: str,
    transcript_window: tuple[str, ...],
    raw_response: str,
    drift_emitted: bool,
    kind: str,
    severity: str,
    detail: str,
    ts: str | None = None,
) -> dict[str, Any]:
    """Assemble one ``judge_io.jsonl`` record (pure except the ``ts`` default).

    ``reasoning_sha256`` hashes the UNCLIPPED ``reasoning_text``;
    ``input.clipped`` is ``True`` iff any input field was truncated.
    """
    sha = hashlib.sha256(reasoning_text.encode("utf-8")).hexdigest()
    clipped_any = False
    reasoning_clipped, clipped = _clip(reasoning_text)
    clipped_any |= clipped
    window: list[str] = []
    for turn in transcript_window:
        text, clipped = _clip(str(turn))
        clipped_any |= clipped
        window.append(text)
    response_clipped, _ = _clip(raw_response)
    return {
        "format_version": JUDGE_IO_FORMAT_VERSION,
        "judge_name": str(judge_name),
        "ts": ts if ts is not None else datetime.now(UTC).isoformat(),
        "call_index": int(call_index),
        "input": {
            "reasoning_text": reasoning_clipped,
            "reasoning_sha256": sha,
            "transcript_window": window,
            "clipped": clipped_any,
        },
        "raw_response": response_clipped,
        "verdict": {
            "drift_emitted": bool(drift_emitted),
            "kind": str(kind),
            "severity": str(severity),
            "detail": str(detail),
        },
    }


@runtime_checkable
class JudgeIOSink(Protocol):
    """Structural protocol a judge-I/O sink must satisfy.

    One method: :meth:`record`, called once per judge ``evaluate`` call
    that reached the LLM (firing AND silent verdicts — the silent ones
    are exactly the missed-fire candidates adjudication needs). The
    caller (:class:`~zicato.judge_runtime.builder._InlineCriterionJudge`)
    wraps every call in its own try/except, but a well-behaved sink
    should also swallow its own I/O failures — capture is best-effort at
    every layer.
    """

    def record(
        self,
        judge_name: str,
        *,
        reasoning_text: str,
        transcript_window: tuple[str, ...],
        raw_response: str,
        drift_emitted: bool,
        kind: str,
        severity: str,
        detail: str,
    ) -> None:
        """Retain one judge evaluate call's verbatim I/O."""
        ...  # pragma: no cover — protocol body


class JudgeIOFileSink:
    """Append-only ``judge_io.jsonl`` sink — one JSON line per record.

    Best-effort: an unwritable path is logged (once per failure, at
    DEBUG) and swallowed; the judge's verdict is never affected. Appends
    are the ``events.jsonl`` durability precedent (an append-only JSONL,
    not a mutable JSON record — the atomic tmp+rename contract applies
    to the latter); the reader tolerates a torn tail line by skipping
    it. ``call_index`` is assigned per sink, monotonically, in call
    order — one sink per run keeps it a per-run sequence.
    """

    __slots__ = ("_path", "_call_index")

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._call_index = 0

    @property
    def path(self) -> Path:
        """The sidecar path this sink appends to."""
        return self._path

    def record(
        self,
        judge_name: str,
        *,
        reasoning_text: str,
        transcript_window: tuple[str, ...],
        raw_response: str,
        drift_emitted: bool,
        kind: str,
        severity: str,
        detail: str,
    ) -> None:
        """Append one record; log-and-continue on any I/O failure."""
        record = build_judge_io_record(
            judge_name=judge_name,
            call_index=self._call_index,
            reasoning_text=reasoning_text,
            transcript_window=transcript_window,
            raw_response=raw_response,
            drift_emitted=drift_emitted,
            kind=kind,
            severity=severity,
            detail=detail,
        )
        self._call_index += 1
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, sort_keys=True) + "\n")
        except OSError as exc:
            log.debug("judge-io capture skipped for %s: %s", judge_name, exc)


def read_judge_io(path: Path) -> list[dict[str, Any]]:
    """Read one ``judge_io.jsonl`` sidecar; empty list on ANY defect.

    The tolerant read twin: a missing/unreadable file returns ``[]``;
    an unparseable line, a non-object line, or a line whose
    ``format_version`` is not :data:`JUDGE_IO_FORMAT_VERSION` (absent,
    older, newer, garbage) is SKIPPED — the reader returns every line it
    can vouch for and never raises.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            body = json.loads(line)
        except (ValueError, json.JSONDecodeError):
            continue
        if not isinstance(body, dict):
            continue
        if body.get("format_version") != JUDGE_IO_FORMAT_VERSION:
            continue
        records.append(body)
    return records


__all__ = [
    "JUDGE_IO_CLIP_CHARS",
    "JUDGE_IO_CLIP_MARKER",
    "JUDGE_IO_FORMAT_VERSION",
    "JudgeIOFileSink",
    "JudgeIOSink",
    "build_judge_io_record",
    "judge_io_path_for_loss",
    "read_judge_io",
]
