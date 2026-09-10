"""The observation corpus — the frozen record board reflection analyzes.

Two producers emit :class:`ObservationRun` records identified by candidate,
entry, measurement purpose, local draw number, and seed. Each record reports
capture fidelity as ``verbatim``, ``result``, or ``preview``; analyzers keep
those levels separate.

Passive (:func:`ingest_lineage`)
--------------------------------
References persisted measurements of each generation's own source, including
tournament, calibration, confirmation, reflection, and admission draws.
Preflight and screening evaluate modified source and are excluded. The reader
validates measurement identity against the loss path and reads matching result
and judge-capture files. Observations retain paths to these artifacts.

Active (:func:`run_corpus`)
---------------------------
Passes ``MeasurementDraw(MeasurementPurpose.REFLECTION, j)`` through the board
context and ``_run_board_units_fast`` arguments. The cache records the runtime
seed. Resuming the same frozen plan reuses complete matching measurements.
An infrastructure-aborted unit raises :class:`ReflectionDrawInconclusive`;
the active run does not publish an outage-derived corpus, and resume retries
the incomplete measurement.



The active run persists ``corpus.jsonl`` (one record per line) and re-writes
the plan with its ``executed`` flag set, which closes the pre-registration
stop/resume seam.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zicato.core import BoardEntry, Generation, RuntimeConfig, ScoringWeights
from zicato.core.measurement import (
    UNKNOWN_SEED,
    BaseSeed,
    MeasurementDraw,
    MeasurementPurpose,
    validate_measurement_count,
)
from zicato.runtime.lock import WorkspaceLock
from zicato.runtime.writer import workspace_writer

#: The three fidelity tiers, strongest first (the capture ladder).
FIDELITY_VERBATIM: str = "verbatim"
FIDELITY_RESULT: str = "result"
FIDELITY_PREVIEW: str = "preview"


class ReflectionDrawInconclusive(RuntimeError):
    """An active-corpus draw hit an INFRA abort — the draw is VOID.

    Mirrors :class:`zicato.tournament.calibration.NoiseFloorInconclusive`: an
    infra abort (worker crash, endpoint outage — never a genuine budget
    exhaustion) is not a measurement of the candidate, so folding its
    worst-case not-completed loss into the corpus would let a transient outage
    poison a reflection. The whole active run VOIDS rather than persist an
    outage-derived corpus; because infra aborts are never cached
    (:func:`zicato.core.loss.is_infra_abort_cause`), a re-run re-attempts the
    voided draw while every clean draw stays a cache HIT.
    """


@dataclass(frozen=True, slots=True)
class ObservationRun:
    """One observed ``(candidate, entry, replicate)`` run in the corpus.

    The BOARD-REFLECTION.md §"data model" schema plus the fidelity tier and
    capture flags. Scalar / loss fields are the *measured* behavior; the
    ``*_ref`` fields REFERENCE the on-disk artifacts (paths, never copies).

    Fields
    ------
    reflection_id, candidate_id, entry_id, replicate:
        The unit's identity within one reflection.
    scalar:
        The single-unit aggregate scalar
        (:func:`zicato.tournament.scoring.aggregate_generation_score` over the
        one entry) — the quantity the reliability bootstrap resamples.
    drift_loss, pass_fail, runtime_ms, aborted, abort_cause:
        The per-run loss surface, straight off the persisted
        :class:`~zicato.core.LossProfile`.
    fidelity:
        :data:`FIDELITY_VERBATIM` (a ``judge_io.jsonl`` sidecar was present) >
        :data:`FIDELITY_RESULT` (a ``result.json`` was present) >
        :data:`FIDELITY_PREVIEW` (neither sidecar was captured).
    has_result, has_judge_io:
        The raw capture flags the fidelity tier derives from.
    loss_ref, transcript_ref:
        Paths to ``loss.json`` and to the transcript source
        (``result.json`` when present, else ``events.jsonl``), or ``None``.
    drift_events:
        ``[{kind, severity, judge_name, count, span_ref}]`` from the loss'
        drift counts (``custom:<judge>`` kinds carry their judge name).
    judge_decisions:
        ``[{judge_name, fired, severity, claim, transcript_span}]`` — from the
        verbatim ``judge_io.jsonl`` records when present (exact firing +
        rationale + a sha256 span ref), else a lower-fidelity reconstruction
        from ``per_judge_loss``.
    loss_decomposition:
        ``{term -> contribution}`` — ``judge:<name>`` from ``per_judge_loss``
        and ``drift:<kind>`` from the severity-weighted drift counts.
    """

    reflection_id: str
    candidate_id: str
    entry_id: str
    replicate: int
    scalar: float
    drift_loss: float
    pass_fail: bool | None
    runtime_ms: int
    aborted: bool
    abort_cause: str | None
    fidelity: str
    has_result: bool
    has_judge_io: bool
    loss_ref: str | None
    transcript_ref: str | None
    drift_events: tuple[dict[str, Any], ...] = ()
    judge_decisions: tuple[dict[str, Any], ...] = ()
    loss_decomposition: dict[str, float] = field(default_factory=dict)
    measurement: MeasurementDraw | None = None

    def __post_init__(self) -> None:
        if self.measurement is not None and self.measurement.draw != self.replicate:
            raise ValueError("observation measurement conflicts with replicate index")

    def to_json(self) -> dict[str, Any]:
        """The one-line ``corpus.jsonl`` shape."""
        return {
            "reflection_id": self.reflection_id,
            "candidate_id": self.candidate_id,
            "entry_id": self.entry_id,
            "replicate": self.replicate,
            "scalar": self.scalar,
            "drift_loss": self.drift_loss,
            "pass_fail": self.pass_fail,
            "runtime_ms": self.runtime_ms,
            "aborted": self.aborted,
            "abort_cause": self.abort_cause,
            "fidelity": self.fidelity,
            "has_result": self.has_result,
            "has_judge_io": self.has_judge_io,
            "loss_ref": self.loss_ref,
            "transcript_ref": self.transcript_ref,
            "drift_events": list(self.drift_events),
            "judge_decisions": list(self.judge_decisions),
            "loss_decomposition": dict(self.loss_decomposition),
            **({"measurement": self.measurement.to_json()} if self.measurement is not None else {}),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ObservationRun:
        """Rebuild one record from its ``corpus.jsonl`` line."""
        return cls(
            reflection_id=str(data.get("reflection_id", "")),
            candidate_id=str(data.get("candidate_id", "")),
            entry_id=str(data.get("entry_id", "")),
            replicate=int(data.get("replicate", 0)),
            scalar=float(data.get("scalar", 0.0)),
            drift_loss=float(data.get("drift_loss", 0.0)),
            pass_fail=data.get("pass_fail"),
            runtime_ms=int(data.get("runtime_ms", 0)),
            aborted=bool(data.get("aborted", False)),
            abort_cause=data.get("abort_cause"),
            fidelity=str(data.get("fidelity", FIDELITY_PREVIEW)),
            has_result=bool(data.get("has_result", False)),
            has_judge_io=bool(data.get("has_judge_io", False)),
            loss_ref=data.get("loss_ref"),
            transcript_ref=data.get("transcript_ref"),
            drift_events=tuple(data.get("drift_events", ())),
            judge_decisions=tuple(data.get("judge_decisions", ())),
            loss_decomposition=dict(data.get("loss_decomposition", {})),
            measurement=MeasurementDraw.from_json(data["measurement"])
            if "measurement" in data
            else None,
        )


# ---------------------------------------------------------------------------
# Record construction (pure over an in-memory LossProfile + sidecar reads)
# ---------------------------------------------------------------------------


def _loss_decomposition(loss: Any, weights: ScoringWeights) -> dict[str, float]:
    """Decompose one run's loss into ``judge:<name>`` + ``drift:<kind>`` terms.

    ``per_judge_loss`` carries the already-per-judge-weighted contribution;
    the drift counts are folded per kind, severity-weighted by
    ``weights.severity_weights`` and scaled by the ``drift:`` channel
    coefficient — the same shape the scalar sums, but kept attributed so a
    dead / dominating term is visible.

    The judge terms are NOT scaled by the ``judge:`` coefficient here: this
    decomposition answers "what did this run's judges say", and scaling both
    channels by their coefficients would make a contract that has turned one
    channel down look like a board on which nothing fired.
    """
    decomp: dict[str, float] = {}
    for jl in getattr(loss, "per_judge_loss", ()) or ():
        name = getattr(jl, "judge_name", "") or "(unattributed)"
        decomp[f"judge:{name}"] = decomp.get(f"judge:{name}", 0.0) + float(
            getattr(jl, "weighted_loss", 0.0)
        )
    sev_weights = getattr(weights, "severity_weights", {}) or {}
    namespace_weights = getattr(weights, "namespace_weights", {}) or {}
    drift_weight = float(namespace_weights.get("drift:", 0.0))
    for dc in loss.metric_counts:
        if not dc.name.startswith("drift:"):
            continue
        sev_w = float(sev_weights.get(getattr(dc, "severity", ""), 1.0))
        key = dc.name
        decomp[key] = decomp.get(key, 0.0) + sev_w * dc.count * drift_weight
    return decomp


def _drift_events(loss: Any) -> tuple[dict[str, Any], ...]:
    """Build the drift-event list from a loss' drift counts.

    ``custom:<judge>`` kinds carry the judge name out separately so the
    coverage / judge-audit analyzers can attribute a drift to its author; no
    span is available in the passive tier (``span_ref`` is ``None``).
    """
    events: list[dict[str, Any]] = []
    for dc in loss.metric_counts:
        if not dc.name.startswith("drift:"):
            continue
        kind = dc.name.removeprefix("drift:")
        judge_name = kind.split(":", 1)[1] if kind.startswith("custom:") else ""
        events.append(
            {
                "kind": kind,
                "severity": str(getattr(dc, "severity", "")),
                "judge_name": judge_name,
                "count": dc.count,
                "span_ref": None,
            }
        )
    return tuple(events)


def _judge_decisions(
    loss: Any, judge_io_records: list[dict[str, Any]]
) -> tuple[dict[str, Any], ...]:
    """Judge decisions — verbatim from ``judge_io.jsonl`` when captured.

    Verbatim (fidelity ``verbatim``): the judge's exact firing, severity,
    one-line claim, and the ``reasoning_sha256`` span ref the adjudicator can
    verify. Fallback (no sidecar): a lower-fidelity ``fired = raw_loss > 0``
    reconstruction from ``per_judge_loss`` (no span, no rationale).

    ``errored`` marks the record of a call that RAISED rather than returning
    a verdict (``kind`` is
    :data:`~zicato.judge_runtime.io_capture.JUDGE_IO_ERROR_KIND`; ``claim``
    then carries the exception text). Without it an adjudicator sees the same
    ``fired: False`` a healthy judge produces and re-reads a broken endpoint
    as a criterion that is too narrow — the exact misdiagnosis reflection
    exists to prevent. The fallback path cannot know: ``per_judge_loss``
    records only judges that fired, so its decisions are ``errored: False``.
    """
    from zicato.judge_runtime.io_capture import JUDGE_IO_ERROR_KIND  # noqa: PLC0415

    if judge_io_records:
        decisions: list[dict[str, Any]] = []
        for rec in judge_io_records:
            verdict = rec["verdict"]
            inp = rec["input"]
            decisions.append(
                {
                    "judge_name": rec["judge_name"],
                    "fired": verdict["drift_emitted"],
                    "errored": verdict["kind"] == JUDGE_IO_ERROR_KIND,
                    "severity": verdict["severity"],
                    "claim": verdict["detail"],
                    "transcript_span": inp["reasoning_sha256"],
                }
            )
        return tuple(decisions)
    return tuple(
        {
            "judge_name": getattr(jl, "judge_name", ""),
            "fired": float(getattr(jl, "raw_loss", 0.0)) > 0.0,
            "errored": False,
            "severity": None,
            "claim": None,
            "transcript_span": None,
        }
        for jl in getattr(loss, "per_judge_loss", ()) or ()
    )


def judge_answered(decision: dict[str, Any]) -> bool:
    """Whether a judge decision is a VERDICT rather than a failed call.

    Every aggregation over ``ObservationRun.judge_decisions`` must filter on
    this first. A decision with ``errored`` set is a call that RAISED (issue
    #121): the judge produced no verdict at all, and its ``fired: False`` is an
    error artifact rather than the judgement "no violation". Folding it in as a
    silent verdict is the misdiagnosis reflection exists to prevent — against a
    ``exhibits`` label it scores as a FALSE NEGATIVE, so a broken judge
    endpoint reads as a judge whose criterion is too narrow, and the
    recommendation that follows ("sharpen the criterion") sends the operator at
    the board when the fix is the judge's model config.

    Records written before the flag existed carry no ``errored`` key and
    read as answered, which is what they were.
    """
    return not bool(decision.get("errored", False))


def _loss_aborted(loss: Any) -> bool:
    """Whether a run aborted — the loss' own ``aborted`` flag, else ``abort_cause``.

    Prefers an explicit ``aborted`` boolean on the loss-like object and falls
    back to ``bool(abort_cause)``. NOTE:
    :class:`~zicato.core.LossProfile` carries no ``aborted`` field (only
    ``abort_cause: str | None`` + ``wall_clock_budget_exceeded``), so the
    fall-back — ``abort_cause`` truthiness — is what fires in practice (a budget
    abort sets ``abort_cause = BUDGET_ABORT_CAUSE``, an infra abort sets its own
    cause). The ``aborted`` probe is a forward-compatible read for any loss-like
    record that DOES expose the flag, so the corpus stays correct if one is
    added later.
    """
    flag = getattr(loss, "aborted", None)
    if flag is not None:
        return bool(flag)
    return bool(getattr(loss, "abort_cause", None))


def _single_unit_scalar(loss: Any, weights: ScoringWeights) -> float:
    """The one-entry aggregate scalar — what the reliability bootstrap resamples."""
    from zicato.tournament.scoring import aggregate_generation_score  # noqa: PLC0415

    agg = aggregate_generation_score([loss], weights)
    return float(agg.get("scalar", 0.0))


def _build_observation(
    *,
    reflection_id: str,
    candidate_id: str,
    entry_id: str,
    replicate: int,
    loss: Any,
    weights: ScoringWeights,
    loss_path: Path,
    result_present: bool,
    events_path: Path | None,
    judge_io_records: list[dict[str, Any]],
) -> ObservationRun:
    """Assemble one :class:`ObservationRun` from a loss + its sidecar reads."""
    has_judge_io = bool(judge_io_records)
    if has_judge_io:
        fidelity = FIDELITY_VERBATIM
    elif result_present:
        fidelity = FIDELITY_RESULT
    else:
        fidelity = FIDELITY_PREVIEW
    result_path = _unit_result_path_for(loss_path)
    if result_present:
        transcript_ref: str | None = str(result_path)
    elif events_path is not None and events_path.exists():
        transcript_ref = str(events_path)
    else:
        transcript_ref = None
    return ObservationRun(
        reflection_id=reflection_id,
        candidate_id=candidate_id,
        entry_id=entry_id,
        replicate=replicate,
        scalar=_single_unit_scalar(loss, weights),
        drift_loss=float(getattr(loss, "drift_loss", 0.0)),
        pass_fail=getattr(loss, "pass_fail", None),
        runtime_ms=int(getattr(loss, "runtime_ms", 0)),
        aborted=_loss_aborted(loss),
        abort_cause=getattr(loss, "abort_cause", None),
        fidelity=fidelity,
        has_result=result_present,
        has_judge_io=has_judge_io,
        loss_ref=str(loss_path),
        transcript_ref=transcript_ref,
        drift_events=_drift_events(loss),
        judge_decisions=_judge_decisions(loss, judge_io_records),
        loss_decomposition=_loss_decomposition(loss, weights),
        measurement=getattr(loss, "measurement", None),
    )


def _unit_result_path_for(loss_path: Path) -> Path:
    from zicato.tournament.unit_cache import unit_result_path  # noqa: PLC0415

    return unit_result_path(loss_path)


def _unit_events_path_for(loss_path: Path) -> Path:
    from zicato.tournament.unit_cache import unit_events_path  # noqa: PLC0415

    return unit_events_path(loss_path)


def _read_sidecars(loss_path: Path, loss: Any) -> tuple[bool, list[dict[str, Any]]]:
    """Read accepted captures; present corruption refuses corpus construction."""
    from zicato.judge_runtime.io_capture import (  # noqa: PLC0415
        judge_io_path_for_loss,
        read_judge_io,
    )
    from zicato.tournament.unit_cache import read_run_result, unit_result_path  # noqa: PLC0415

    result_present = read_run_result(unit_result_path(loss_path), expected=loss) is not None
    judge_io_records = read_judge_io(judge_io_path_for_loss(loss_path), expected=loss)
    return result_present, judge_io_records


def _read_loss(loss_path: Path) -> Any | None:
    """Read one ``loss.json`` via the reducer; ``None`` on any defect."""
    from zicato.tournament.unit_cache import read_capture_loss  # noqa: PLC0415

    try:
        return read_capture_loss(loss_path)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Passive ingest — zero LLM, references the lineage's artifacts
# ---------------------------------------------------------------------------


def ingest_lineage(
    *,
    workspace_root: Path,
    epoch_id: str,
    reflection_id: str,
    candidates: tuple[str, ...] | list[str],
    entries: tuple[str, ...] | list[str],
    weights: ScoringWeights,
) -> list[ObservationRun]:
    """Build the corpus from already-persisted lineage artifacts. Zero LLM.

    For each candidate and entry, select validated measurements of that
    generation’s own source. Each :class:`ObservationRun` retains its purpose,
    local draw, seed, and artifact paths. Judge capture supplies ``verbatim``
    fidelity; result capture supplies ``result``; otherwise use ``preview``.
    Missing captures reduce fidelity; corrupt present captures refuse construction.
    """
    from zicato.core.workspace import run_dir as _run_dir  # noqa: PLC0415
    from zicato.tournament.unit_cache import own_code_board_draws  # noqa: PLC0415

    runs: list[ObservationRun] = []
    for candidate_id in candidates:
        for entry_id in entries:
            run_directory = _run_dir(workspace_root, epoch_id, candidate_id, entry_id)
            # Include only measurements of the recorded generation's own
            # source. Preflight and screening evaluate modified source.
            for replicate, loss_path in own_code_board_draws(run_directory):
                loss = _read_loss(loss_path)
                if loss is None:
                    continue
                result_present, judge_io_records = _read_sidecars(loss_path, loss)
                runs.append(
                    _build_observation(
                        reflection_id=reflection_id,
                        candidate_id=candidate_id,
                        entry_id=entry_id,
                        replicate=replicate.draw,
                        loss=loss,
                        weights=weights,
                        loss_path=loss_path,
                        result_present=result_present,
                        events_path=_unit_events_path_for(loss_path),
                        judge_io_records=judge_io_records,
                    )
                )
    return runs


# ---------------------------------------------------------------------------
# Active scheduler — board_reflection purpose with one local draw per sample
# ---------------------------------------------------------------------------


async def run_corpus(
    *,
    adapter: Any,
    plan: Any,
    generations: list[Generation],
    board: list[BoardEntry],
    weights: ScoringWeights,
    config: RuntimeConfig,
    workspace_root: Path,
    disable_drift: tuple[Any, ...] = (),
    judge_only: bool = False,
    persist: bool = True,
    writer: WorkspaceLock | None = None,
) -> list[ObservationRun]:
    """Collect board-reflection measurements and optionally publish the corpus.

    Each candidate evaluates the selected entries at local draws from zero to
    ``plan.replicates - 1`` under the ``board_reflection`` purpose. The runner
    carries measurement identity through task context, cache keys, and seeded
    artifact paths. Resume reuses complete matching draws. An infrastructure
    abort raises :class:`ReflectionDrawInconclusive` and prevents publication
    of an outage-derived corpus.

    With ``persist=True``, write ``corpus.jsonl`` and mark the plan executed.
    Return the :class:`ObservationRun` list in either mode.
    """

    validate_measurement_count(plan.replicates)
    from zicato.tournament.worker_execution import drain_worker_cleanup  # noqa: PLC0415

    async with workspace_writer(
        workspace_root,
        writer=writer,
        instance_id=config.instance_id,
        cleanup=lambda: drain_worker_cleanup(workspace_root),
    ) as writer:
        from zicato.core.loss import is_infra_abort_cause  # noqa: PLC0415
        from zicato.tournament.scheduling import _run_board_units_fast  # noqa: PLC0415
        from zicato.tournament.worker_transport import (  # noqa: PLC0415
            _stamp_disable_drift,
            _stamp_judge_only,
            _stamp_measurement,
        )

        entry_ids = set(plan.entries)
        board_subset = (
            [entry for entry in board if entry.id in entry_ids] if entry_ids else list(board)
        )
        stamped_board = _stamp_judge_only(
            _stamp_disable_drift(board_subset, disable_drift), judge_only
        )

        runs: list[ObservationRun] = []
        for generation in generations:
            for draw in range(int(plan.replicates)):
                measurement = MeasurementDraw(MeasurementPurpose.REFLECTION, draw)
                losses = await _run_board_units_fast(
                    writer=writer,
                    adapter=adapter,
                    child_gen=generation,
                    board=_stamp_measurement(stamped_board, measurement),
                    weights=weights,
                    config=config,
                    workspace_root=workspace_root,
                    epoch_id=plan.epoch_id,
                    match_id=f"reflection:{plan.reflection_id}:r{draw}",
                    measurement=measurement,
                )
                # Same discipline as the preflight's degraded draw: an infra abort
                # makes the draw un-measurable rather than worst-case — void it
                # rather than persist an outage-derived observation. Infra aborts
                # are never cached, so a re-run re-attempts this same slot.
                if any(
                    is_infra_abort_cause(getattr(lp, "abort_cause", None)) for lp in losses.values()
                ):
                    raise ReflectionDrawInconclusive(
                        f"reflection {plan.reflection_id}: candidate {generation.id} draw r{draw} "
                        "hit an infra abort (endpoint outage / worker crash); the draw is "
                        "inconclusive and must not be persisted."
                    )
                for entry in board_subset:
                    loss = losses.get(entry.id)
                    if loss is None:
                        continue
                    loss_path = _active_loss_path(
                        workspace_root,
                        plan.epoch_id,
                        generation.id,
                        entry.id,
                        measurement,
                        base_seed=config.seed,
                    )
                    result_present, judge_io_records = _read_sidecars(loss_path, loss)
                    runs.append(
                        _build_observation(
                            reflection_id=plan.reflection_id,
                            candidate_id=generation.id,
                            entry_id=entry.id,
                            replicate=measurement.draw,
                            loss=loss,
                            weights=weights,
                            loss_path=loss_path,
                            result_present=result_present,
                            events_path=_unit_events_path_for(loss_path),
                            judge_io_records=judge_io_records,
                        )
                    )

        if persist:
            write_corpus(workspace_root, plan.epoch_id, plan.reflection_id, runs)
            from zicato.reflection.plan import write_plan  # noqa: PLC0415

            write_plan(workspace_root, plan.mark_executed())
        return runs


def _active_loss_path(
    workspace_root: Path,
    epoch_id: str,
    generation_id: str,
    entry_id: str,
    measurement: MeasurementDraw,
    *,
    base_seed: BaseSeed = UNKNOWN_SEED,
) -> Path:
    from zicato.tournament.unit_cache import _unit_loss_path  # noqa: PLC0415

    return _unit_loss_path(
        workspace_root, epoch_id, generation_id, entry_id, measurement, base_seed=base_seed
    )


# ---------------------------------------------------------------------------
# corpus.jsonl persistence
# ---------------------------------------------------------------------------


def write_corpus(
    workspace_root: Path,
    epoch_id: str,
    reflection_id: str,
    runs: list[ObservationRun],
) -> Path:
    """Persist the corpus as ``corpus.jsonl`` (one record per line), atomically."""
    from zicato.core.workspace import reflection_corpus_path  # noqa: PLC0415

    path = reflection_corpus_path(workspace_root, epoch_id, reflection_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        "".join(json.dumps(run.to_json(), sort_keys=True) + "\n" for run in runs),
        encoding="utf-8",
    )
    os.replace(tmp, path)
    return path


def read_corpus(
    workspace_root: Path,
    epoch_id: str,
    reflection_id: str,
) -> list[ObservationRun]:
    """Read ``corpus.jsonl`` back into records; ``[]`` on absence.

    Tolerant of a torn / unparseable / non-object line (skipped) — the corpus
    is a derived artifact; a re-run re-materialises it.
    """
    from zicato.core.workspace import reflection_corpus_path  # noqa: PLC0415

    path = reflection_corpus_path(workspace_root, epoch_id, reflection_id)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    runs: list[ObservationRun] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            body = json.loads(line)
        except (ValueError, json.JSONDecodeError):
            continue
        if isinstance(body, dict):
            runs.append(ObservationRun.from_json(body))
    return runs


__all__ = [
    "FIDELITY_PREVIEW",
    "FIDELITY_RESULT",
    "FIDELITY_VERBATIM",
    "ObservationRun",
    "ReflectionDrawInconclusive",
    "ingest_lineage",
    "judge_answered",
    "read_corpus",
    "run_corpus",
    "write_corpus",
]
