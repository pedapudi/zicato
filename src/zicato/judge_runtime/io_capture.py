"""Verbatim judge-call captures beside each unit's loss and events.

The file sink appends one record for each answered or failed judge call.
Capture failures cannot change the verdict or fail the run. Readers distinguish
absent captures from malformed complete rows, retaining only a torn append's
valid prefix. Paired loss identity controls whether a capture grants fidelity.

Text fields are clipped; reasoning_sha256 identifies the unclipped input.
Only inline model-backed judges use this sink. Operator-owned Python judges
publish verdict events without a captured model call.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from zicato.core.loss import LossProfile, capture_matches_loss
from zicato.core.measurement import MeasurementDraw, artifact_measurement, unit_artifact_name
from zicato.epoch._storage import RecordError, check_record_format

log = logging.getLogger("zicato.judge_runtime.io_capture")

#: Supported format of each complete judge capture row.
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
    index = artifact_measurement(name)
    if index is not None:
        return loss_path.with_name(unit_artifact_name("judge_io", index))
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


def judge_io_record_from_payload(payload: object) -> dict[str, Any]:
    """Validate one complete judge capture without dropping extension fields."""
    if not isinstance(payload, dict):
        raise RecordError("judge capture must be an object")
    check_record_format(payload, "judge capture", expected_version=JUDGE_IO_FORMAT_VERSION)
    if any(not isinstance(payload.get(name), str) for name in ("judge_name", "ts", "raw_response")):
        raise RecordError("judge capture names, timestamp and response must be text")
    if type(payload.get("call_index")) is not int or payload["call_index"] < 0:
        raise RecordError("judge capture call index must be a nonnegative integer")
    inp, verdict = payload.get("input"), payload.get("verdict")
    if not isinstance(inp, dict) or not isinstance(verdict, dict):
        raise RecordError("judge capture input and verdict must be objects")
    window, sha256 = inp.get("transcript_window"), inp.get("reasoning_sha256")
    if (
        not isinstance(inp.get("reasoning_text"), str)
        or not isinstance(window, list)
        or any(not isinstance(turn, str) for turn in window)
        or type(inp.get("clipped")) is not bool
        or not isinstance(sha256, str)
        or len(sha256) != 64
        or any(char not in "0123456789abcdef" for char in sha256)
    ):
        raise RecordError("judge capture input has invalid text, digest or clipping metadata")
    if type(verdict.get("drift_emitted")) is not bool or any(
        not isinstance(verdict.get(name), str) for name in ("kind", "severity", "detail")
    ):
        raise RecordError("judge capture verdict has invalid fields")
    if "measurement" in payload:
        try:
            MeasurementDraw.from_json(payload["measurement"])
        except ValueError as exc:
            raise RecordError(str(exc)) from exc
        if not isinstance(payload.get("run_id"), str) or not payload["run_id"]:
            raise RecordError("judge capture measurement requires its run identity")
    elif "run_id" in payload and not isinstance(payload["run_id"], str):
        raise RecordError("judge capture run identity must be text")
    return payload


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
    measurement: MeasurementDraw | None = None,
    run_id: str | None = None,
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
    payload: dict[str, Any] = {
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
    if measurement is not None:
        payload["measurement"] = measurement.to_json()
    if run_id is not None:
        payload["run_id"] = run_id
    return judge_io_record_from_payload(payload)


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

    The worker supplies fixed measurement and run identity at construction;
    every line carries those values independently of judge input.
    """

    __slots__ = ("_path", "_call_index", "_measurement", "_run_id")

    def __init__(
        self, path: Path, *, measurement: MeasurementDraw | None = None, run_id: str | None = None
    ) -> None:
        self._path = Path(path)
        self._call_index = 0
        self._measurement = measurement
        self._run_id = run_id

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
            measurement=self._measurement,
            run_id=self._run_id,
        )
        self._call_index += 1
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, sort_keys=True) + "\n")
        except OSError as exc:
            log.debug("judge-io capture skipped for %s: %s", judge_name, exc)


def read_judge_io(path: Path, *, expected: LossProfile | None = None) -> list[dict[str, Any]]:
    """Read complete judge rows, retaining an interrupted append's valid prefix.

    Absence returns an empty list. Malformed complete records raise RecordError;
    only undecodable JSON or UTF-8 in a final unterminated append is ignored.
    Valid rows that do not match the paired measurement remain audit-readable
    through an unpaired read, but cannot supply that measurement's fidelity.
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise RecordError(f"judge capture {path}: {exc}") from exc
    records: list[dict[str, Any]] = []
    lines = raw.split(b"\n")
    for position, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            if position == len(lines) - 1 and not raw.endswith(b"\n"):
                break
            raise RecordError(f"judge capture {path} line {position + 1}: {exc}") from exc
        try:
            body = judge_io_record_from_payload(value)
        except (ValueError, RecordError) as exc:
            raise RecordError(f"judge capture {path} line {position + 1}: {exc}") from exc
        if capture_matches_loss(body, expected):
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
    "judge_io_record_from_payload",
]
