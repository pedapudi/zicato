"""Measurement reuse and provenance for tournament scheduling.

A reusable unit is identified by its epoch, generation, board entry,
measurement purpose, draw, and selected seed. A seed-specific request
requires matching persisted provenance and evidence that execution started.
Historical records remain readable without becoming seed-specific cache hits.

This module owns cache admission, scheduling-omission records, descriptive
draw enumeration, and reuse. Replicate reduction is owned by tournament scoring.
Forced remeasurement retains prior
artifacts through the complete archive in :mod:`zicato.tournament.artifacts`.
Capture companions remain diagnostic records rather than scoring inputs.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from zicato.core import (
    BoardEntry,
    Generation,
    LossProfile,
)
from zicato.core.loss import capture_matches_loss, has_execution_evidence, validate_loss_identity
from zicato.core.measurement import (
    UNKNOWN_SEED,
    BaseSeed,
    MeasurementDraw,
    artifact_replicate_index,
    iter_measurement_artifacts,
    measurement_artifact_path,
    range_at,
    recorded_artifact_measurement,
    recorded_measurement,
    unit_artifact_name,
)
from zicato.core.workspace import run_coordinates_from_dir
from zicato.epoch._storage import RecordError, check_record_format
from zicato.tournament.scoring import average_replicate_losses as _average_losses
from zicato.tournament.worker_transport import _run_id_for

log = logging.getLogger("zicato.tournament.runner")


def _telemetry_helpers() -> tuple[Any, Any]:
    """Resolve the telemetry sink/reducer pair via the runner module.

    The cache read/write path reads the reducer through
    ``zicato.tournament.runner._telemetry_helpers`` — an attribute access on
    the runner module object (NOT a bound import) so the test suite, which
    monkeypatches ``runner._telemetry_helpers`` to swap in a stub reducer,
    still drives this cache layer. The runner re-exports the canonical
    :func:`zicato.tournament.worker_transport._telemetry_helpers`, so an
    unpatched call returns that canonical pair. The import is function-local
    so there is no import-time cycle: the runner imports this module rather
    than the other way round at load time.
    """
    from zicato.tournament import runner  # noqa: PLC0415

    pair: tuple[Any, Any] = runner._telemetry_helpers()  # type: ignore[attr-defined]
    return pair


def _skipped_unit_loss(
    *,
    generation: Generation,
    entry: BoardEntry,
    epoch_id: str,
    match_id: str,
) -> LossProfile:
    """Record a scheduling omission without claiming a task measurement."""
    return LossProfile(
        run_id=_run_id_for(generation, entry),
        entry_id=entry.id,
        generation_id=generation.id,
        epoch_id=epoch_id,
        drift_counts=(),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=0,
        expectation_result=None,
        drift_loss=0.0,
        match_id=match_id,
        execution_started=False,
        wall_clock_budget_exceeded=False,
        not_completed_reason="scheduling_budget_exhausted",
        pass_fail=None,
    )


def _unit_loss_path(
    workspace_root: Path,
    epoch_id: str,
    generation_id: str,
    entry_id: str,
    replicate_index: int,
    *,
    base_seed: BaseSeed = UNKNOWN_SEED,
) -> Path:
    """Locate one purpose/draw slot under its selected seed directory.

    Historical records omit the seed directory. Explicit unseeded executions
    use seed-none; integer seeds use seed-<integer>. The integer filename
    encoding remains the same within every seed directory."""
    from zicato.core.workspace import loss_profile_path  # noqa: PLC0415

    canonical = loss_profile_path(workspace_root, epoch_id, generation_id, entry_id)
    return measurement_artifact_path(canonical.parent, "loss", replicate_index, base_seed=base_seed)


#: Current replicate transcripts used by the best-available capture reader.
_EVENTS_REPLICATE_RE = re.compile(r"^events\.r(\d+)\.jsonl$")


def is_own_code_board_draw(replicate_index: int) -> bool:
    """Whether the registered purpose measures this generation's own code.

    File enumeration also checks the recorded identity; a slot alone does not
    establish historical provenance. Unclaimed slots supply no evidence.
    """
    allocation = range_at(replicate_index)
    return allocation is not None and allocation.own_code


def _loss_slots(
    run_dir: Path, keep: Callable[[int], bool], *, include_aliases: bool = False
) -> list[tuple[int, Path]]:
    """The persisted loss slots of ONE run dir that ``keep`` admits, ascending.

    THE filename walk of a run directory: ``loss.json`` → replicate 0,
    ``loss.r{n}.json`` → replicate ``n``. An attempt sibling
    (``loss.a1.json``, ``loss.r2.a1.json``) matches neither form, so a
    superseded execution is excluded by construction for every caller.
    """
    return sorted(
        (index, path)
        for path in iter_measurement_artifacts(run_dir, include_aliases=include_aliases)
        if (index := artifact_replicate_index(path.name, canonical=not include_aliases)) is not None
        and keep(index)
    )


def own_code_board_draws(
    run_dir: Path, *, base_seed: BaseSeed = UNKNOWN_SEED
) -> list[tuple[int, Path]]:
    """Enumerate eligible own-code draws, optionally restricted to one seed.

    Every physical slot must agree with its recorded identity. An explicit
    seed includes None for unseeded executions and excludes unknown history.
    With no seed filter, descriptive readers may inspect all eligible records;
    statistical admission still requires complete provenance."""
    _, reducer_module = _telemetry_helpers()
    draws = []
    seen: set[MeasurementDraw] = set()
    coordinates = run_coordinates_from_dir(run_dir)
    for index, path in _loss_slots(run_dir, is_own_code_board_draw):
        try:
            loss = reducer_module.read_loss_profile(path)
            measurement = recorded_artifact_measurement(
                run_dir, path, loss.measurement, loss.match_id
            )
            if coordinates is not None:
                validate_loss_identity(
                    loss,
                    epoch_id=coordinates[0],
                    generation_id=coordinates[1],
                    entry_id=coordinates[2],
                    measurement=measurement,
                )
        except (OSError, ValueError, KeyError):
            continue
        if (
            measurement not in seen
            and has_execution_evidence(loss)
            and (base_seed is UNKNOWN_SEED or measurement.base_seed == base_seed)
        ):
            seen.add(measurement)
            draws.append((index, path))
    return draws


def persisted_loss_slots(run_dir: Path) -> list[tuple[int, Path]]:
    """Every persisted loss slot under ONE run dir, ascending: records rather than evidence.

    The unfiltered twin of :func:`own_code_board_draws`, and the two answer
    different questions of the same directory.

    :func:`own_code_board_draws` is an EVIDENCE read: it admits only the
    bands whose draws are that generation's own code over the real board, so
    a deliberately-degraded pre-flight probe cached beside the real draws can
    never reach a reader as champion behaviour. Anything that asks "what did
    this generation do" belongs there.

    This walk is a RECORDS pass: it names every slot a maintenance command
    must keep internally consistent, refused bands included, because a
    persisted profile whose fields disagree with its own drift counts is a
    wrong record no matter which owner wrote it. It carries no claim about
    what the draws measured, so it must never stand in for the evidence
    filter. Attempt siblings stay excluded either way — they record
    executions that were superseded rather than slots.
    """
    return _loss_slots(run_dir, lambda _index: True, include_aliases=True)


#: Supported format of complete result captures; absent provenance remains historical.
RUN_RESULT_FORMAT_VERSION: int = 1

#: Per-field clip for the persisted RunResult text (256 KiB). Each transcript
#: turn and ``final_output`` longer than this is truncated with
#: :data:`RUN_RESULT_CLIP_MARKER` appended and the record's ``clipped`` flag
#: set — the artifact stays bounded no matter what the harness emitted.
RUN_RESULT_CLIP_CHARS: int = 262144

#: Marker appended to every clipped text field in ``result.json``.
RUN_RESULT_CLIP_MARKER: str = " … [truncated]"


def unit_result_path(loss_path: Path) -> Path:
    """Map ONE board unit's ``loss.json`` path to its ``result.json`` twin.

    Pure sibling-name math mirroring :func:`_unit_loss_path`'s replicate
    slotting: ``loss.json`` → ``result.json`` (the canonical replicate-0
    slot, also :func:`zicato.core.workspace.run_result_path`) and
    ``loss.r{n}.json`` → ``result.r{n}.json``. Taking the LOSS path (not
    the coordinates) keeps the two artifacts glued to the same replicate
    slot by construction — a caller cannot pair replicate 3's loss with
    replicate 0's result.
    """
    name = loss_path.name
    index = artifact_replicate_index(name)
    if index is not None:
        return loss_path.with_name(unit_artifact_name("result", index))
    if name.startswith("loss."):
        return loss_path.with_name("result." + name[len("loss.") :])
    # Defensive: an unexpected filename still gets a deterministic sibling.
    return loss_path.with_name("result.json")


def unit_events_path(loss_path: Path) -> Path:
    """Map one replicate's loss path to its events JSONL twin."""
    index = artifact_replicate_index(loss_path.name)
    return loss_path.with_name(unit_artifact_name("events", index or 0))


def any_unit_transcript(canonical_events_path: Path) -> Path:
    """Pick the transcript that best represents ONE generation×entry.

    Readers that answer "what did this generation DO on this entry" — the
    proposer's redacted facts, its process exemplars, the failure-pattern
    detector — want a transcript rather than a specific replicate. Each draw
    keeps its own ``events.jsonl``, so a reader naming one fixed file would
    have to pick a draw. Naming replicate 0
    alone would blind them whenever the only draws so far are the contract
    pre-flight's probe and the calibration band — which is exactly the state
    at the FIRST round's proposal, before any duel has run.

    Preference order, first non-empty file winning:

    1. the canonical replicate-0 ``events.jsonl`` — a real duel draw;
    2. own-code full-board draws in ascending replicate order
       (:func:`is_own_code_board_draw`), so a calibration draw of the
       candidate's real code is preferred over a degraded probe;
    3. any remaining replicate, ascending.

    Returns the canonical path unchanged when nothing readable exists, so a
    caller's "no telemetry" branch behaves exactly as it did before.
    """
    run_dir = canonical_events_path.parent

    def _has_content(path: Path) -> bool:
        try:
            return path.is_file() and path.stat().st_size > 0
        except OSError:
            return False

    if _has_content(canonical_events_path):
        return canonical_events_path
    if not run_dir.is_dir():
        return canonical_events_path

    own_code: list[tuple[int, Path]] = []
    other: list[tuple[int, Path]] = []
    for path in run_dir.iterdir():
        match = _EVENTS_REPLICATE_RE.match(path.name)
        if not match or not _has_content(path):
            continue
        index = int(match.group(1))
        (own_code if is_own_code_board_draw(index) else other).append((index, path))
    for candidates in (own_code, other):
        if candidates:
            return min(candidates)[1]
    return canonical_events_path


def _clip_result_text(text: str) -> tuple[str, bool]:
    """Clip one ``result.json`` text field; return ``(text, was_clipped)``."""
    if len(text) <= RUN_RESULT_CLIP_CHARS:
        return text, False
    return text[:RUN_RESULT_CLIP_CHARS] + RUN_RESULT_CLIP_MARKER, True


def run_result_from_payload(payload: object) -> dict[str, Any]:
    """Accept the result capture schema, retaining extensions and absent provenance.

    All required fields were present in the first version-one producer.
    Measurement and artifact summaries remain optional historical additions.
    """
    if not isinstance(payload, dict):
        raise RecordError("result capture must be an object")
    check_record_format(
        payload, "result capture", expected_version=RUN_RESULT_FORMAT_VERSION, allow_missing=False
    )
    for name in ("run_id", "entry_id", "final_output", "abort_reason"):
        if not isinstance(payload.get(name), str):
            raise RecordError(f"result capture {name} must be text")
    turns = payload.get("transcript")
    if not isinstance(turns, list) or any(not isinstance(turn, str) for turn in turns):
        raise RecordError("result capture transcript must contain text turns")
    if type(payload.get("runtime_ms")) is not int or payload["runtime_ms"] < 0:
        raise RecordError("result capture runtime must be a nonnegative integer")
    if any(type(payload.get(name)) is not bool for name in ("aborted", "clipped")):
        raise RecordError("result capture flags must be booleans")
    if "measurement" in payload:
        try:
            MeasurementDraw.from_json(payload["measurement"])
        except ValueError as exc:
            raise RecordError(str(exc)) from exc
    if "artifacts" in payload:
        artifacts = payload["artifacts"]
        if not isinstance(artifacts, dict):
            raise RecordError("result artifact summary must be an object")
        for name in ("root", "manifest"):
            value = artifacts.get(name)
            if not isinstance(value, str):
                raise RecordError("result artifact names must be text")
        for name in ("file_count", "total_bytes"):
            if type(artifacts.get(name)) is not int or artifacts[name] < 0:
                raise RecordError("result artifact counts must be nonnegative integers")
        if type(artifacts.get("truncated")) is not bool:
            raise RecordError("result artifact truncation must be a boolean")
    return payload


def run_result_to_payload(
    run_result: Any, *, measurement: MeasurementDraw | None = None
) -> dict[str, Any]:
    """Build the ``result.json`` payload for one run's ``RunResult``.

    Pure: no I/O. The payload is the RunResult's user-facing surface —
    NOTHING beyond what :class:`zicato.core.RunResult` already exposes
    (its docstring's collusion exclusion is preserved: internal agent
    reasoning / tool calls / goldfive events stay in ``events.jsonl``).
    Every transcript turn and ``final_output`` is clipped at
    :data:`RUN_RESULT_CLIP_CHARS` with :data:`RUN_RESULT_CLIP_MARKER`
    appended; ``clipped`` is ``True`` iff any field was truncated.
    """
    clipped_any = False
    final_output, clipped = _clip_result_text(str(run_result.final_output))
    clipped_any |= clipped
    transcript: list[str] = []
    for turn in run_result.transcript:
        text, clipped = _clip_result_text(str(turn))
        clipped_any |= clipped
        transcript.append(text)
    artifacts = getattr(run_result, "artifacts", None)
    payload = {
        "format_version": RUN_RESULT_FORMAT_VERSION,
        "run_id": str(run_result.run_id),
        "entry_id": str(run_result.entry_id),
        "final_output": final_output,
        "transcript": transcript,
        "runtime_ms": int(run_result.runtime_ms),
        "aborted": bool(run_result.aborted),
        "abort_reason": str(run_result.abort_reason),
        "clipped": clipped_any,
    }
    if measurement is not None:
        payload["measurement"] = measurement.to_json()
    if artifacts is not None:
        payload["artifacts"] = {
            "root": artifacts.root.name,
            "manifest": artifacts.manifest_path.name,
            "file_count": len(artifacts.files),
            "total_bytes": artifacts.total_bytes,
            "truncated": artifacts.truncated,
        }
    return run_result_from_payload(payload)


def read_capture_loss(loss_path: Path) -> LossProfile | None:
    """Read paired loss; absence permits audit reads, present defects do not.

    Retained attempts follow the same rule. Callers that decline invalid
    captures may catch ``ValueError`` and fall back to lower-fidelity evidence.
    """
    from zicato.telemetry.reducer import read_loss_profile  # noqa: PLC0415

    try:
        return read_loss_profile(loss_path)
    except FileNotFoundError:
        return None
    except (OSError, KeyError, ValueError, TypeError, AttributeError, OverflowError) as exc:
        raise ValueError(f"paired loss unavailable at {loss_path}: {exc}") from exc


def read_run_result(path: Path, *, expected: LossProfile | None = None) -> dict[str, Any] | None:
    """Read a supported capture; missing or ineligible captures return None.

    Malformed present records raise RecordError and remain available for audit.
    Structural acceptance precedes measurement eligibility. A known-seed paired
    loss requires matching measurement and run identity, including in archives.
    """
    try:
        body = run_result_from_payload(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, ValueError, RecordError) as exc:
        raise RecordError(f"result capture {path}: {exc}") from exc
    if not capture_matches_loss(body, expected):
        return None
    if "measurement" in body:
        try:
            measurement = MeasurementDraw.from_json(body["measurement"])
            index = artifact_replicate_index(path.name, "result")
            if index is not None:
                recorded_measurement(index, measurement=measurement)
        except ValueError as exc:
            raise RecordError(f"result capture {path}: {exc}") from exc
    return body


def _resolve_cached_unit(
    *,
    workspace_root: Path,
    epoch_id: str,
    generation_id: str,
    entry_id: str,
    replicate_index: int,
    base_seed: BaseSeed = UNKNOWN_SEED,
) -> LossProfile | None:
    """Resolve a completed draw for one generation, entry, purpose, and seed.

    Epoch and generation identify the sealed contract and immutable code.
    The selected base seed is an additional causal input: historical records
    with no seed provenance cannot satisfy an explicitly seeded or unseeded
    request. Recorded purpose, draw, and seed must agree with the file path.
    Missing, malformed, conflicting, or unstarted records produce a cache
    miss while their original artifacts remain available for audit."""
    _, reducer_module = _telemetry_helpers()
    historical = _unit_loss_path(workspace_root, epoch_id, generation_id, entry_id, replicate_index)
    path = measurement_artifact_path(
        historical.parent, "loss", replicate_index, base_seed=base_seed
    )
    if not path.exists():
        return None
    try:
        loss: LossProfile = reducer_module.read_loss_profile(path)
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        return None
    try:
        measurement = recorded_artifact_measurement(
            historical.parent, path, loss.measurement, loss.match_id
        )
        validate_loss_identity(
            loss,
            epoch_id=epoch_id,
            generation_id=generation_id,
            entry_id=entry_id,
            measurement=measurement,
        )
    except ValueError as exc:
        log.warning("%s at %s; retained for audit and excluded from cache reuse", exc, path)
        return None
    if not has_execution_evidence(loss):
        if loss.execution_started is not False:
            log.warning(
                "ambiguous historical budget record %s has no execution evidence; "
                "retaining the record and retrying the unit",
                path,
            )
        return None
    return loss


def _persist_unit_loss(
    *,
    workspace_root: Path,
    epoch_id: str,
    generation_id: str,
    entry_id: str,
    replicate_index: int,
    loss: LossProfile,
) -> None:
    """Persist an executed draw in the slot identified by its measurement.

    Unstarted scheduling omissions become attempt siblings. A real worker
    already wrote the profile; this write also supports in-process adapters
    and preserves idempotence. The execution owner archives displaced
    profiles and their companions before a rerun starts."""
    measurement = loss.measurement or MeasurementDraw.from_index(replicate_index)
    if loss.measurement is not None:
        recorded_measurement(replicate_index, measurement=loss.measurement)
    loss = replace(loss, measurement=measurement)
    if loss.execution_started is False:
        record_unit_attempt(
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            generation_id=generation_id,
            entry_id=entry_id,
            replicate_index=replicate_index,
            loss=loss,
        )
        return
    _, reducer_module = _telemetry_helpers()
    writer = getattr(reducer_module, "write_loss_profile", None)
    if not callable(writer):
        # The reducer in this environment exposes no writer (e.g. a test
        # stub that only reads). Nothing to persist — the next lookup is a
        # correct MISS, and the worker's own canonical loss.json (when the
        # real worker ran) is still on disk for replicate 0.
        return
    path = _unit_loss_path(
        workspace_root,
        epoch_id,
        generation_id,
        entry_id,
        replicate_index,
        base_seed=measurement.base_seed,
    )
    try:
        writer(loss, path)
    except OSError as exc:  # noqa: BLE001 — cache persist is best-effort
        log.debug(
            "unit-loss cache persist skipped for %s/%s r%d: %s",
            generation_id,
            entry_id,
            replicate_index,
            exc,
        )


#: Matches the ``.a{n}`` infix an attempt sibling carries (see
#: :func:`_next_attempt_path`), anchored at the end of the file stem so it
#: cannot collide with a replicate slot.
_ATTEMPT_SLOT_RE = re.compile(r"\.a\d+$")


def is_unit_attempt_slot(path: Path) -> bool:
    """``True`` iff ``path`` names an attempt sibling rather than a scoring slot.

    The guard for any reader that reaches a run directory by GLOB rather
    than by exact name: an attempt file is provenance about an execution
    that was superseded, so it must never be read as a replicate's
    measurement.
    """
    return bool(_ATTEMPT_SLOT_RE.search(path.stem))


def _next_attempt_path(loss_path: Path) -> Path:
    """The next free attempt sibling of a canonical loss slot.

    ``loss.json`` → ``loss.a1.json``, ``loss.a2.json``, …; a replicate slot
    keeps its own series (``loss.r2.json`` → ``loss.r2.a1.json``), so an
    attempt is never confused with a replicate. The ``.a{n}`` infix sits
    where no reader of a replicate slot looks: every one of them matches
    ``loss.json`` exactly or parses the ``r{digits}`` between ``loss.`` and
    ``.json`` (:func:`zicato.reflection.corpus._discover_replicate_losses`,
    :func:`zicato.query.eval_view._cell_evidence_replicate_index`), and
    ``r2.a1`` is not digits. Attempts therefore never enter scoring,
    reflection ingest, or the evidence count.

    The index is one past however many siblings already exist, so a unit
    that failed twice reads back as ``a1``, ``a2``, then the canonical file.
    """
    stem = loss_path.stem
    existing = sum(1 for _ in loss_path.parent.glob(f"{stem}.a*.json"))
    return loss_path.with_name(f"{stem}.a{existing + 1}.json")


def record_unit_attempt(
    *,
    workspace_root: Path,
    epoch_id: str,
    generation_id: str,
    entry_id: str,
    replicate_index: int,
    loss: LossProfile | None = None,
    base_seed: BaseSeed = UNKNOWN_SEED,
) -> None:
    """Record ONE non-final execution of a board unit beside its cache slot.

    A board unit is evaluated at most once for SCORING, but it can be
    executed more than once: an infra abort is deliberately not cached so
    the next need re-attempts it, and ``--mode full`` re-measures a unit
    every round. Only the last execution survives in the canonical slot, so
    without this record a unit that failed twice and then passed is
    indistinguishable from one that passed first time — the difference
    between a healthy harness and a flaky one.

    Two callers, one for each way an execution stops being the final one:

    * ``loss`` supplied — the execution just settled and its profile will
      NOT be persisted (the infra-abort path). Nothing durable exists for
      it yet, so the profile is written to the attempt slot. It carries its
      own ``abort_cause`` / ``not_completed_reason``, which is the record of
      why the attempt failed; no ``result.json`` twin is copied, because on
      this path any twin on disk belongs to an EARLIER execution of the
      slot rather than to this one.
    * ``loss`` omitted — the canonical slot is about to be overwritten by a
      re-run (the ``force_fresh`` path). Both the persisted profile and its
      ``result.json`` twin are copied aside first; copied rather than moved
      so the slot keeps answering cache reads unchanged if the re-run never
      writes. The profile alone also reaches ``loss.archive.jsonl`` (see
      :func:`archive_outgoing_unit_loss`); the attempt slot is what keeps
      its ``result.json`` twin and gives the execution an addressable pair.

    Provenance only: nothing here is a scoring input, and best-effort
    throughout — a failed write must never cost a round.
    """
    if loss is not None and loss.measurement is not None:
        base_seed = loss.measurement.base_seed
    path = _unit_loss_path(
        workspace_root, epoch_id, generation_id, entry_id, replicate_index, base_seed=base_seed
    )
    try:
        if loss is not None:
            loss = replace(
                loss, measurement=MeasurementDraw.from_index(replicate_index, base_seed=base_seed)
            )
            _, reducer_module = _telemetry_helpers()
            writer = getattr(reducer_module, "write_loss_profile", None)
            if not callable(writer):
                return
            writer(loss, _next_attempt_path(path))
            return
        if not path.exists():
            return
        attempt_path = _next_attempt_path(path)
        attempt_path.write_bytes(path.read_bytes())
        result_path = unit_result_path(path)
        if result_path.exists():
            unit_result_path(attempt_path).write_bytes(result_path.read_bytes())
    except OSError as exc:  # noqa: BLE001 — attempt records are best-effort
        log.debug(
            "unit attempt record skipped for %s/%s r%d: %s",
            generation_id,
            entry_id,
            replicate_index,
            exc,
        )


#: Append-only archive of the per-entry loss profiles a re-measurement
#: overwrote, one JSON line per displaced profile, in the run directory
#: beside the canonical ``loss.json`` / ``loss.r<n>.json`` slots.
LOSS_ARCHIVE_FILENAME = "loss.archive.jsonl"


def _replicate_index_from_slot(path: Path) -> int:
    """The replicate index a loss-slot filename encodes — inverse of :func:`_unit_loss_path`.

    ``loss.json`` is replicate 0; ``loss.r<n>.json`` is replicate ``n``.
    An unrecognised name reads as 0 rather than raising: the index is
    provenance on an archive record, never a lookup key.
    """
    return artifact_replicate_index(path.name) or 0


def archive_outgoing_unit_loss(path: Path) -> None:
    """Append the displaced profile before execution replaces its artifacts.

    The complete artifact owner calls this after publishing the retained copy
    and before clearing the reusable slot. The raw profile remains available
    to the historical decoder, with its slot coordinates and ordered ``seq``.

    The slot filename supplies the replicate index.

    An empty slot is a no-op. Unreadable prior data or an unwritable archive
    does not prevent the caller from publishing its canonical loss.
    """
    if not path.exists():
        return
    try:
        outgoing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.debug("unit-loss archive skipped (unreadable %s): %s", path, exc)
        return
    if not isinstance(outgoing, dict):
        return
    archive = path.with_name(LOSS_ARCHIVE_FILENAME)
    seq = 0
    try:
        with open(archive, encoding="utf-8") as fh:
            seq = sum(1 for line in fh if line.strip())
    except OSError:
        seq = 0
    record = {
        "seq": seq,
        "slot": path.name,
        "replicate_index": _replicate_index_from_slot(path),
        "profile": outgoing,
    }
    try:
        with open(archive, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str, sort_keys=True) + "\n")
    except OSError as exc:  # pragma: no cover — unwritable workspace
        log.debug("unit-loss archive append skipped for %s: %s", archive, exc)


def read_unit_loss_history(
    workspace_root: Path,
    epoch_id: str,
    generation_id: str,
    entry_id: str,
    replicate_index: int = 0,
    *,
    base_seed: BaseSeed = UNKNOWN_SEED,
) -> list[LossProfile]:
    """Every measurement of ONE board unit, oldest first.

    The displaced profiles from ``loss.archive.jsonl`` (in write order)
    followed by whatever occupies the canonical slot NOW — so the last
    element is always the profile :func:`_resolve_cached_unit` would
    serve, and the earlier ones are the measurements that preceded it
    (issue #122). A unit measured exactly once yields a single-element
    list; a unit never measured yields an empty one.

    Best-effort, like every other reader on this path: an unreadable
    archive line is skipped rather than raising, so a partially written
    record cannot wedge an analysis.
    """
    from zicato.telemetry.reducer import (  # noqa: PLC0415 — avoid import cycle
        loss_profile_from_dict,
        read_loss_profile,
    )

    path = _unit_loss_path(
        workspace_root, epoch_id, generation_id, entry_id, replicate_index, base_seed=base_seed
    )
    history: list[LossProfile] = []
    archive = path.with_name(LOSS_ARCHIVE_FILENAME)
    try:
        lines = archive.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or record.get("slot") not in (None, path.name):
            continue
        profile = record.get("profile")
        if not isinstance(profile, dict):
            continue
        try:
            history.append(loss_profile_from_dict(profile))
        except (KeyError, TypeError, ValueError):
            continue
    if path.exists():
        try:
            history.append(read_loss_profile(path))
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            pass
    return history


@dataclass(frozen=True, slots=True)
class _UnitProvenance:
    """Per-generation tally of cached-vs-fresh board-unit evaluations.

    Additive runtime provenance: how many of a generation's board units
    this duel were reused from the cache (``cached``) vs genuinely
    executed (``fresh``). Surfaced on :attr:`TournamentResult.unit_provenance`
    so a structure-agnostic caller can attribute reuse to the CHAMPION
    specifically and to the journal so an operator sees how much a fast
    round reused. Never a contract input.
    """

    cached: int = 0
    fresh: int = 0

    def with_hit(self) -> _UnitProvenance:
        return _UnitProvenance(cached=self.cached + 1, fresh=self.fresh)

    def with_miss(self) -> _UnitProvenance:
        return _UnitProvenance(cached=self.cached, fresh=self.fresh + 1)


def _record_provenance(
    provenance: dict[str, _UnitProvenance] | None,
    generation_id: str,
    *,
    cached: bool,
) -> None:
    """Fold one board unit's cached/fresh outcome into the per-gen tally."""
    if provenance is None:
        return
    current = provenance.get(generation_id, _UnitProvenance())
    provenance[generation_id] = current.with_hit() if cached else current.with_miss()


__all__ = [
    "LOSS_ARCHIVE_FILENAME",
    "RUN_RESULT_CLIP_CHARS",
    "RUN_RESULT_CLIP_MARKER",
    "RUN_RESULT_FORMAT_VERSION",
    "_UnitProvenance",
    "_average_losses",
    "_persist_unit_loss",
    "_record_provenance",
    "_resolve_cached_unit",
    "_skipped_unit_loss",
    "_unit_loss_path",
    "archive_outgoing_unit_loss",
    "is_own_code_board_draw",
    "is_unit_attempt_slot",
    "own_code_board_draws",
    "persisted_loss_slots",
    "any_unit_transcript",
    "read_run_result",
    "run_result_from_payload",
    "read_unit_loss_history",
    "record_unit_attempt",
    "run_result_to_payload",
    "unit_events_path",
    "unit_result_path",
]
