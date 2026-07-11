"""The observation corpus — the frozen record board reflection analyzes.

Two producers, one record type. Both emit :class:`ObservationRun` — one per
``(candidate, entry, replicate)`` unit — and both stay honest about *fidelity*:
every record is stamped ``verbatim`` / ``result`` / ``preview`` per the R1
capture ladder (BOARD-REFLECTION.md), and the downstream analyzers aggregate
tiers separately (a verbatim finding outranks a preview one).

Passive (:func:`ingest_lineage`)
--------------------------------
Zero LLM budget. Walks the epoch's generations and REFERENCES the run
artifacts the loop already persisted — reads ``loss.json`` (+ every
``loss.r{n}`` replicate, so the A/A calibration slots at base ``1000`` and any
prior reflection draws at base ``5000`` come along as **free pillar-1
replicates**), and the R1 sidecars ``result.json`` and ``judge_io.jsonl`` via
their tolerant readers. It stores PATHS, never copies bytes — the corpus is a
lens over the lineage, not a duplicate of it.

Active (:func:`run_corpus`)
---------------------------
Mirrors :func:`zicato.epoch.preflight.run_contract_preflight` EXACTLY in shape:
``_stamp_judge_only(_stamp_disable_drift(board, ...))`` then
``_stamp_replicate_index(board, 5000 + j)`` and ``_run_board_units_fast(...,
match_id="reflection:{id}:r{j}", replicate_index=5000 + j)`` — reflection's
reserved row in the replicate-base ledger (0 duels, 1000 calibration, 2000
preflight, 3000/3001 screening, 4000 evidence, **5000 reflection**). The
schedulers' universal per-unit cache makes it idempotent: a re-run of the same
frozen plan is all cache HITs at the same slots (no ``_run_single`` call). An
infra abort on any unit VOIDS that draw with
:class:`ReflectionDrawInconclusive` — infra aborts are never cached
(:func:`zicato.core.loss.is_infra_abort_cause`), so a re-run re-attempts
cleanly, mirroring the preflight's ``NoiseFloorInconclusive`` discipline.

The active run persists ``corpus.jsonl`` (one record per line) and re-writes
the plan with its ``executed`` flag set — the pre-registration stop/resume
seam closes here.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zicato.core import BoardEntry, Generation, RuntimeConfig, ScoringWeights

#: Replicate-index base for active board-reflection draws. Reserved far above
#: every other owner in the partitioned replicate namespace so a reflection
#: draw's cache slot can never collide with — or pre-seed — anything a
#: tournament, calibration, preflight, screen, or evidence gate reads:
#:
#:   * ``0..``           tournament duels (r0 is the canonical ``loss.json``)
#:   * ``1000``          A/A noise-floor calibration
#:   * ``2000``          contract pre-flight degraded probe
#:   * ``3000`` / ``3001`` candidate screen (+ confirm-before-veto)
#:   * ``4000``          evidence gate (both-sides-fresh)
#:   * ``5000``          board reflection (this owner)
#:
#: Draw ``j`` of a (candidate, entry) unit runs, caches, and stamps its harness
#: noise draw at ``REFLECTION_REPLICATE_BASE + j``. See dev-guide ch. 04
#: §"reserved bases" beside ``epoch/preflight.py``'s ``PREFLIGHT_REPLICATE_BASE``.
REFLECTION_REPLICATE_BASE: int = 5000

#: The three fidelity tiers, strongest first (the R1 ladder).
FIDELITY_VERBATIM: str = "verbatim"
FIDELITY_RESULT: str = "result"
FIDELITY_PREVIEW: str = "preview"

_LOSS_REPLICATE_RE = re.compile(r"^loss\.r(\d+)\.json$")


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
        :data:`FIDELITY_PREVIEW` (neither — historical / stubbed run).
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
        )


# ---------------------------------------------------------------------------
# Record construction (pure over an in-memory LossProfile + sidecar reads)
# ---------------------------------------------------------------------------


def _loss_decomposition(loss: Any, weights: ScoringWeights) -> dict[str, float]:
    """Decompose one run's loss into ``judge:<name>`` + ``drift:<kind>`` terms.

    ``per_judge_loss`` carries the already-weighted per-judge contribution;
    the drift counts are folded per kind, severity-weighted by
    ``weights.severity_weights`` and scaled by ``weights.drift_weight`` — the
    same shape the reducer sums into the scalar, but kept attributed so a
    dead / dominating term is visible.
    """
    decomp: dict[str, float] = {}
    for jl in getattr(loss, "per_judge_loss", ()) or ():
        name = getattr(jl, "judge_name", "") or "(unattributed)"
        decomp[f"judge:{name}"] = decomp.get(f"judge:{name}", 0.0) + float(
            getattr(jl, "weighted_loss", 0.0)
        )
    sev_weights = getattr(weights, "severity_weights", {}) or {}
    drift_weight = float(getattr(weights, "drift_weight", 0.0))
    for dc in getattr(loss, "drift_counts", ()) or ():
        sev_w = float(sev_weights.get(getattr(dc, "severity", ""), 1.0))
        key = f"drift:{getattr(dc, 'kind', '')}"
        decomp[key] = decomp.get(key, 0.0) + sev_w * int(getattr(dc, "count", 0)) * drift_weight
    return decomp


def _drift_events(loss: Any) -> tuple[dict[str, Any], ...]:
    """Build the drift-event list from a loss' drift counts.

    ``custom:<judge>`` kinds carry the judge name out separately so the
    coverage / judge-audit analyzers can attribute a drift to its author; no
    span is available in the passive tier (``span_ref`` is ``None``).
    """
    events: list[dict[str, Any]] = []
    for dc in getattr(loss, "drift_counts", ()) or ():
        kind = str(getattr(dc, "kind", ""))
        judge_name = kind.split(":", 1)[1] if kind.startswith("custom:") else ""
        events.append(
            {
                "kind": kind,
                "severity": str(getattr(dc, "severity", "")),
                "judge_name": judge_name,
                "count": int(getattr(dc, "count", 0)),
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
    """
    if judge_io_records:
        decisions: list[dict[str, Any]] = []
        for rec in judge_io_records:
            verdict = rec.get("verdict", {}) if isinstance(rec, dict) else {}
            inp = rec.get("input", {}) if isinstance(rec, dict) else {}
            decisions.append(
                {
                    "judge_name": str(rec.get("judge_name", "")),
                    "fired": bool(verdict.get("drift_emitted", False)),
                    "severity": str(verdict.get("severity", "")),
                    "claim": str(verdict.get("detail", "")),
                    "transcript_span": inp.get("reasoning_sha256"),
                }
            )
        return tuple(decisions)
    return tuple(
        {
            "judge_name": getattr(jl, "judge_name", ""),
            "fired": float(getattr(jl, "raw_loss", 0.0)) > 0.0,
            "severity": None,
            "claim": None,
            "transcript_span": None,
        }
        for jl in getattr(loss, "per_judge_loss", ()) or ()
    )


def _loss_aborted(loss: Any) -> bool:
    """Whether a run aborted — the loss' own ``aborted`` flag, else ``abort_cause``.

    Prefers an explicit ``aborted`` boolean on the loss-like object and falls
    back to ``bool(abort_cause)``. NOTE: today's
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
    )


def _unit_result_path_for(loss_path: Path) -> Path:
    from zicato.tournament.unit_cache import unit_result_path  # noqa: PLC0415

    return unit_result_path(loss_path)


def _read_sidecars(loss_path: Path) -> tuple[bool, list[dict[str, Any]]]:
    """``(result_present, judge_io_records)`` via the tolerant R1 readers."""
    from zicato.judge_runtime.io_capture import (  # noqa: PLC0415
        judge_io_path_for_loss,
        read_judge_io,
    )
    from zicato.tournament.unit_cache import read_run_result, unit_result_path  # noqa: PLC0415

    result_present = read_run_result(unit_result_path(loss_path)) is not None
    judge_io_records = read_judge_io(judge_io_path_for_loss(loss_path))
    return result_present, judge_io_records


def _read_loss(loss_path: Path) -> Any | None:
    """Read one ``loss.json`` via the reducer; ``None`` on any defect."""
    from zicato.telemetry import reducer  # noqa: PLC0415

    if not loss_path.exists():
        return None
    try:
        return reducer.read_loss_profile(loss_path)
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        return None


def _is_ingestable_replicate(index: int) -> bool:
    """Whether a replicate-base slot is honest evidence for the passive corpus.

    The replicate-index namespace is partitioned by owner (see
    :data:`REFLECTION_REPLICATE_BASE`). Passive ingest accepts ONLY the slots
    whose provenance the ledger vouches for as a clean, non-degraded draw of the
    REAL contract:

      * ``0``            — the canonical tournament duel (``loss.json``).
      * ``1000..1999``   — A/A noise-floor calibration (``CALIBRATION_REPLICATE_BASE``);
                           free pillar-1 replicates of the real board.
      * ``4000..4999``   — evidence gate (``EVIDENCE_REPLICATE_BASE``); both-sides-fresh
                           clean draws of real trees.
      * ``>= 5000``      — board reflection (this owner); prior reflection draws.

    EXCLUDED — ``2000..3999``:

      * ``2000`` (``PREFLIGHT_REPLICATE_BASE``) is the contract pre-flight's
        DELIBERATELY-DEGRADED champion probe, cached under the champion's OWN
        generation id — folding it in would poison the corpus with a
        known-bad draw of a mutilated board.
      * ``3000``/``3001`` (``SCREEN_REPLICATE_BASE``) are candidate-screen
        bases — fast-mode probes, not clean duels; excluded defensively.

    Any other unreserved slot (``1..999``) is excluded too: the corpus ingests
    only slots the ledger names, never an unattributed one.
    """
    if index == 0:
        return True
    if 1000 <= index <= 1999:
        return True
    if 2000 <= index <= 3999:
        return False
    if 4000 <= index <= 4999:
        return True
    return index >= 5000


def _discover_replicate_losses(run_dir: Path) -> list[tuple[int, Path]]:
    """Every INGESTABLE persisted replicate loss under one run dir, ascending.

    ``loss.json`` → replicate 0; ``loss.r{n}.json`` → replicate ``n``. Only
    slots the reserved-base ledger vouches for are returned
    (:func:`_is_ingestable_replicate`): r0, the calibration slots at 1000+, the
    evidence slots at 4000+, and any prior reflection draws at 5000+ — never the
    pre-flight's degraded r2000 probe or the 3000s screen bases.
    """
    found: list[tuple[int, Path]] = []
    canonical = run_dir / "loss.json"
    if canonical.exists():
        found.append((0, canonical))
    if run_dir.is_dir():
        for path in run_dir.iterdir():
            match = _LOSS_REPLICATE_RE.match(path.name)
            if match:
                index = int(match.group(1))
                if _is_ingestable_replicate(index):
                    found.append((index, path))
    return sorted(found)


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

    For every ``(candidate, entry)`` pair, every persisted replicate slot
    (``loss.json`` + every ``loss.r{n}`` — the calibration and prior-reflection
    draws are free pillar-1 replicates) becomes one :class:`ObservationRun`
    that REFERENCES its ``loss.json`` / ``result.json`` / ``events.jsonl`` by
    path. The R1 sidecars decide fidelity: a ``judge_io.jsonl`` sidecar ⇒
    ``verbatim``, else a ``result.json`` ⇒ ``result``, else ``preview``.
    Missing / unreadable artifacts degrade the record, never crash.
    """
    from zicato.core.workspace import run_dir as _run_dir  # noqa: PLC0415

    runs: list[ObservationRun] = []
    for candidate_id in candidates:
        for entry_id in entries:
            run_directory = _run_dir(workspace_root, epoch_id, candidate_id, entry_id)
            events_path = run_directory / "events.jsonl"
            for replicate, loss_path in _discover_replicate_losses(run_directory):
                loss = _read_loss(loss_path)
                if loss is None:
                    continue
                result_present, judge_io_records = _read_sidecars(loss_path)
                runs.append(
                    _build_observation(
                        reflection_id=reflection_id,
                        candidate_id=candidate_id,
                        entry_id=entry_id,
                        replicate=replicate,
                        loss=loss,
                        weights=weights,
                        loss_path=loss_path,
                        result_present=result_present,
                        events_path=events_path,
                        judge_io_records=judge_io_records,
                    )
                )
    return runs


# ---------------------------------------------------------------------------
# Active scheduler — mirrors run_contract_preflight's shape, base 5000 + j
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
) -> list[ObservationRun]:
    """Produce fresh corpus draws at the reserved base; persist + mark executed.

    For every candidate generation and every replicate ``j`` in
    ``range(plan.replicates)``, runs the plan's board entries through
    :func:`zicato.tournament.scheduling._run_board_units_fast` at
    ``REFLECTION_REPLICATE_BASE + j`` — stamped and keyed EXACTLY as the
    preflight stamps its degraded draw. The schedulers' per-unit cache makes a
    re-run of the same frozen plan all HITs (no ``_run_single``); a draw whose
    any unit hit an infra abort raises :class:`ReflectionDrawInconclusive`
    (the whole active run voids rather than persist an outage-derived corpus).

    With ``persist`` (the default) the corpus is written to ``corpus.jsonl``
    and the plan is re-written with its ``executed`` flag set — the
    pre-registration resume seam. Returns the in-memory
    :class:`ObservationRun` list either way.
    """
    from zicato.core.loss import is_infra_abort_cause  # noqa: PLC0415
    from zicato.tournament.scheduling import _run_board_units_fast  # noqa: PLC0415
    from zicato.tournament.worker_transport import (  # noqa: PLC0415
        _stamp_disable_drift,
        _stamp_judge_only,
        _stamp_replicate_index,
    )

    entry_ids = set(plan.entries)
    board_subset = [entry for entry in board if entry.id in entry_ids] if entry_ids else list(board)
    stamped_board = _stamp_judge_only(_stamp_disable_drift(board_subset, disable_drift), judge_only)

    runs: list[ObservationRun] = []
    for generation in generations:
        for draw in range(int(plan.replicates)):
            replicate_index = REFLECTION_REPLICATE_BASE + draw
            losses = await _run_board_units_fast(
                adapter=adapter,
                child_gen=generation,
                board=_stamp_replicate_index(stamped_board, replicate_index),
                weights=weights,
                config=config,
                workspace_root=workspace_root,
                epoch_id=plan.epoch_id,
                match_id=f"reflection:{plan.reflection_id}:r{draw}",
                replicate_index=replicate_index,
            )
            # Same discipline as the preflight's degraded draw: an infra abort
            # makes the draw un-measurable, not worst-case — void it rather than
            # persist an outage-derived observation. Infra aborts are never
            # cached, so a re-run re-attempts exactly this slot.
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
                    workspace_root, plan.epoch_id, generation.id, entry.id, replicate_index
                )
                result_present, judge_io_records = _read_sidecars(loss_path)
                events_path = loss_path.parent / "events.jsonl"
                runs.append(
                    _build_observation(
                        reflection_id=plan.reflection_id,
                        candidate_id=generation.id,
                        entry_id=entry.id,
                        replicate=replicate_index,
                        loss=loss,
                        weights=weights,
                        loss_path=loss_path,
                        result_present=result_present,
                        events_path=events_path,
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
    replicate_index: int,
) -> Path:
    from zicato.tournament.unit_cache import _unit_loss_path  # noqa: PLC0415

    return _unit_loss_path(workspace_root, epoch_id, generation_id, entry_id, replicate_index)


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
    "REFLECTION_REPLICATE_BASE",
    "ObservationRun",
    "ReflectionDrawInconclusive",
    "ingest_lineage",
    "read_corpus",
    "run_corpus",
    "write_corpus",
]
