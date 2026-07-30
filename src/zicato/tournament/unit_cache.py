"""Per-unit loss cache + provenance for the tournament schedulers.

A **board unit** is the atomic, contract-fixed quantum
``(generation_id, board_entry_id, replicate_index)``. Under a fixed
contract its result is immutable, so it must be evaluated AT MOST ONCE
and reused everywhere — every pairing, every round, every structure, the
gate, and later evolve rounds. This module owns that universal,
structure-agnostic cache:

* the per-replicate ``loss.json`` path mapping (:func:`_unit_loss_path`);
* the read/write of a cached unit (:func:`_resolve_cached_unit`,
  :func:`_persist_unit_loss`);
* the budget-skip synthesis that records an un-run unit as a
  cache-eligible budget-exceeded loss (:func:`_skipped_unit_loss`);
* the per-generation cached-vs-fresh provenance tally
  (:class:`_UnitProvenance`, :func:`_record_provenance`);
* the replicate fold that collapses N paired runs into one per-entry
  loss map (:func:`_average_losses`) — the replication primitive, which
  must aggregate every scalar-bearing field because scoring never sees
  the individual replicates;
* the ``result.json`` twin — the persisted RunResult capture that rides
  the same replicate slotting as ``loss.json``
  (:func:`unit_result_path`, :func:`run_result_to_payload`,
  :func:`read_run_result`; written best-effort by the worker, read by
  board reflection — never a scoring input).

Extracted verbatim from :mod:`zicato.tournament.runner`, which
re-exports this module's public surface so existing
``from zicato.tournament.runner import ...`` imports keep working
unchanged.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zicato.core import (
    BUDGET_ABORT_CAUSE,
    BoardEntry,
    Generation,
    JudgeLoss,
    LossProfile,
    MetricCount,
    MetricSeverity,
    ScoringWeights,
)
from zicato.tournament.worker_transport import (
    _aborted_loss_profile,
    _run_id_for,
)

log = logging.getLogger("zicato.tournament.runner")


def _telemetry_helpers() -> tuple[Any, Any]:
    """Resolve the telemetry sink/reducer pair via the runner module.

    The cache read/write path reads the reducer through
    ``zicato.tournament.runner._telemetry_helpers`` — an attribute access on
    the runner module object (NOT a bound import) so the test suite, which
    monkeypatches ``runner._telemetry_helpers`` to swap in a stub reducer,
    still drives this cache layer. The runner re-exports the canonical
    :func:`zicato.tournament.worker_transport._telemetry_helpers`, so an
    unpatched call returns exactly the same pair as before the extraction.
    The import is function-local so there is no import-time cycle (the runner
    imports this module, not the other way round at load time).
    """
    from zicato.tournament import runner  # noqa: PLC0415

    pair: tuple[Any, Any] = runner._telemetry_helpers()  # type: ignore[attr-defined]
    return pair


def _skipped_unit_loss(
    *,
    generation: Generation,
    entry: BoardEntry,
    epoch_id: str,
    weights: ScoringWeights,
    match_id: str,
) -> LossProfile:
    """Synthesise a budget-exceeded :class:`LossProfile` for an un-run unit.

    A board unit that was never LAUNCHED because the matchup's wall-clock
    budget was already spent is recorded exactly like a unit whose worker
    was killed at its deadline: :func:`_aborted_loss_profile` with
    ``wall_clock_budget_exceeded=True`` and zero runtime (no subprocess
    ever ran). Reusing that path keeps the scoring + cache semantics
    identical — the skipped unit aggregates as a worst-case loss for its
    side, and persisting it makes it a cache HIT on the next need.
    """
    return _aborted_loss_profile(
        run_id=_run_id_for(generation, entry),
        entry=entry,
        generation_id=generation.id,
        epoch_id=epoch_id,
        weights=weights,
        runtime_ms=0,
        match_id=match_id,
        # A unit skipped because the matchup's wall-clock budget was already
        # spent IS a genuine budget exhaustion — re-running would re-hit the
        # same cap — so it is cache-eligible (the one cacheable abort cause).
        abort_cause=BUDGET_ABORT_CAUSE,
    )


def _unit_loss_path(
    workspace_root: Path,
    epoch_id: str,
    generation_id: str,
    entry_id: str,
    replicate_index: int,
) -> Path:
    """Return the per-replicate cache path for ONE board unit's ``loss.json``.

    A **board unit** is the atomic, contract-fixed quantum
    ``(generation_id, board_entry_id, replicate_index)`` — under a fixed
    contract its result is immutable, so it must be evaluated AT MOST
    ONCE and reused everywhere (every pairing, every round, every
    structure, the gate, later evolve rounds).

    Replicate 0 maps to the canonical ``runs/<entry>/loss.json`` the
    worker writes (back-compat: existing caches, the seed champion's
    full-board scoring, and every single-replicate run land there).
    Replicate r>0 maps to a sibling ``runs/<entry>/loss.r<r>.json`` so
    the additional noise samples cache per replicate without colliding
    with the canonical file. The directory is the same per-entry run
    directory either way; only the filename varies by replicate.
    """
    from zicato.core.workspace import loss_profile_path  # noqa: PLC0415

    canonical = loss_profile_path(workspace_root, epoch_id, generation_id, entry_id)
    if replicate_index <= 0:
        return canonical
    return canonical.with_name(f"loss.r{replicate_index}.json")


#: ``format_version`` stamped onto every persisted ``result.json``. Readers
#: accept exactly this version and return ``None`` for anything else — a
#: missing / older / newer / garbage file degrades to "no capture", never a
#: crash (the file is a best-effort reflection artifact, not a scoring input).
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
    if name.startswith("loss."):
        return loss_path.with_name("result." + name[len("loss.") :])
    # Defensive: an unexpected filename still gets a deterministic sibling.
    return loss_path.with_name("result.json")


def _clip_result_text(text: str) -> tuple[str, bool]:
    """Clip one ``result.json`` text field; return ``(text, was_clipped)``."""
    if len(text) <= RUN_RESULT_CLIP_CHARS:
        return text, False
    return text[:RUN_RESULT_CLIP_CHARS] + RUN_RESULT_CLIP_MARKER, True


def run_result_to_payload(run_result: Any) -> dict[str, Any]:
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
    return {
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


def read_run_result(path: Path) -> dict[str, Any] | None:
    """Read one persisted ``result.json``; ``None`` on ANY defect.

    The tolerant read twin of the worker's best-effort write: a missing
    file (legacy run, opted-out runtime, failed capture), unreadable
    bytes, non-JSON / non-object content, or a ``format_version`` other
    than :data:`RUN_RESULT_FORMAT_VERSION` (absent, older, newer,
    garbage) all return ``None`` — the caller degrades to the next
    fidelity tier (BOARD-REFLECTION.md's ladder), never crashes.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        body = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(body, dict):
        return None
    if body.get("format_version") != RUN_RESULT_FORMAT_VERSION:
        return None
    return body


def _resolve_cached_unit(
    *,
    workspace_root: Path,
    epoch_id: str,
    generation_id: str,
    entry_id: str,
    replicate_index: int,
) -> LossProfile | None:
    """Resolve ONE board unit from its persisted per-replicate ``loss.json``.

    The universal, structure-agnostic cache lookup keyed on
    ``(generation_id, entry_id, replicate_index)`` within the epoch. A
    generation is immutable and belongs to exactly one epoch/contract, so
    the on-disk ``epochs/<epoch>/generations/<gen>/runs/<entry>/loss.json``
    (per replicate) IS the contract-scoped cache for that unit — a
    different contract is a fresh epoch with fresh generations, a natural
    miss (no cross-contract reuse).

    Returns the cached :class:`LossProfile` on a HIT (the unit is then
    NOT executed), or ``None`` on a MISS — the file is absent or
    unreadable. An unreadable file is a miss, not a crash: the caller
    re-runs the unit and re-persists, so the next need is a hit.

    This resolves for ANY generation — the champion AND every challenger
    — replacing the champion-only ``_resolve_cached_champion_losses``: a
    competitor's board run is reused across all its pairings/rounds, the
    champion is reused if already evaluated under this epoch/contract, and
    prior evals carry across ``--rounds`` in the same epoch.
    """
    _, reducer_module = _telemetry_helpers()
    path = _unit_loss_path(workspace_root, epoch_id, generation_id, entry_id, replicate_index)
    if not path.exists():
        return None
    try:
        return reducer_module.read_loss_profile(path)  # type: ignore[no-any-return]
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        return None


def _persist_unit_loss(
    *,
    workspace_root: Path,
    epoch_id: str,
    generation_id: str,
    entry_id: str,
    replicate_index: int,
    loss: LossProfile,
) -> None:
    """Persist ONE board unit's loss to its per-replicate cache path.

    Called after a genuine cache MISS runs the unit, so the next need for
    the same ``(generation, entry, replicate)`` is a HIT. For replicate 0
    the canonical worker-written ``loss.json`` already exists; rewriting
    it with the identical profile is idempotent (and makes the cache
    consistent even when the unit ran via a test stub that did not write).
    For replicate r>0 this is the only writer of the sibling
    ``loss.r<r>.json``. Best-effort: a write failure degrades the next
    lookup to another (correct) MISS rather than aborting the tournament.
    """
    _, reducer_module = _telemetry_helpers()
    writer = getattr(reducer_module, "write_loss_profile", None)
    if not callable(writer):
        # The reducer in this environment exposes no writer (e.g. a test
        # stub that only reads). Nothing to persist — the next lookup is a
        # correct MISS, and the worker's own canonical loss.json (when the
        # real worker ran) is still on disk for replicate 0.
        return
    path = _unit_loss_path(workspace_root, epoch_id, generation_id, entry_id, replicate_index)
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


def _mean_over_present(values: list[float | None]) -> float | None:
    """Mean of the values that are present; ``None`` when none are.

    The "not measured is not zero" fold used for optional continuous
    fields (:attr:`LossProfile.score`, per-key
    :attr:`LossProfile.metrics`): a replicate that produced no value does
    not drag the mean toward zero, it simply does not vote. ``None`` is
    returned only when EVERY replicate abstained, so an entry with no
    expectation folds to ``None`` exactly as it did before replication.
    """
    present = [float(v) for v in values if v is not None]
    if not present:
        return None
    return sum(present) / len(present)


def _mean_metrics(profiles: list[LossProfile]) -> dict[str, float] | None:
    """Fold the per-entry ``metrics`` decomposition across replicates.

    Each key is meaned over the replicates that REPORT it (the
    "not measured is not zero" model of :func:`_mean_over_present`) —
    a scorer that emitted ``precision`` on three of four replicates
    reports the mean of those three. Returns ``None`` when no replicate
    carried a decomposition, so a board whose scorers expose none folds
    byte-identically to the pre-replication path.

    This exists so the folded decomposition actually decomposes the
    folded :attr:`LossProfile.score` beside it. Carrying replicate 0's
    ``metrics`` next to an averaged ``score`` would be the one option
    that is actively misleading.
    """
    keys: list[str] = []
    seen: set[str] = set()
    for p in profiles:
        for key in p.metrics or {}:
            if key not in seen:
                seen.add(key)
                keys.append(key)
    if not keys:
        return None
    folded: dict[str, float] = {}
    for key in keys:
        mean = _mean_over_present([(p.metrics or {}).get(key) for p in profiles])
        if mean is not None:
            folded[key] = mean
    return folded


def _mean_metric_counts(profiles: list[LossProfile]) -> tuple[MetricCount, ...]:
    """Fold the namespaced ``metric_counts`` view across replicates.

    Each ``(name, severity)`` bucket is meaned over ALL replicates, with
    an absent bucket contributing ``0.0``. That divisor is deliberate: it
    is exactly the per-run-mean model
    :func:`~zicato.tournament.scoring.aggregate_namespaced_metrics` uses
    ("a loss with none contributes zero"), so the namespace aggregate
    computed over the folded profiles equals the aggregate computed over
    every replicate run individually. Using a present-only divisor here
    would inflate a sparse namespace by the number of replicates that
    never saw it.

    Bucket ORDER is the first-seen order across replicates, so the fold
    is deterministic and replicate 0's ordering is preserved for the
    buckets it carried.
    """
    keys: list[tuple[str, MetricSeverity]] = []
    seen: set[tuple[str, MetricSeverity]] = set()
    for p in profiles:
        for mc in p.metric_counts:
            key = (mc.name, mc.severity)
            if key not in seen:
                seen.add(key)
                keys.append(key)
    if not keys:
        return ()
    n = len(profiles)
    folded: list[MetricCount] = []
    for name, severity in keys:
        total = 0.0
        for p in profiles:
            for mc in p.metric_counts:
                if mc.name == name and mc.severity == severity:
                    total += float(mc.count)
        folded.append(MetricCount(name=name, severity=severity, count=total / n))
    return tuple(folded)


def _mean_per_judge_loss(profiles: list[LossProfile]) -> tuple[JudgeLoss, ...]:
    """Fold the per-judge loss attribution across replicates.

    ``raw_loss`` / ``weighted_loss`` are meaned over ALL replicates with
    an absent judge contributing zero — the same divisor
    :func:`~zicato.tournament.scoring._per_judge_loss_aggregate` applies
    ("a judge absent from a run contributes zero to its sum"), so the
    per-judge aggregate carried onto
    :class:`~zicato.scoring.api.ScalarContext` is the same whether it is
    taken over the folded profiles or over every replicate run. ``weight``
    is the contract's per-judge multiplier — constant across replicates of
    one epoch — so the first replicate that reports the judge supplies it.
    """
    order: list[str] = []
    weights: dict[str, float] = {}
    for p in profiles:
        for jl in p.per_judge_loss:
            if jl.judge_name not in weights:
                order.append(jl.judge_name)
                weights[jl.judge_name] = jl.weight
    if not order:
        return ()
    n = len(profiles)
    folded: list[JudgeLoss] = []
    for name in order:
        raw_total = 0.0
        weighted_total = 0.0
        for p in profiles:
            for jl in p.per_judge_loss:
                if jl.judge_name == name:
                    raw_total += float(jl.raw_loss)
                    weighted_total += float(jl.weighted_loss)
        folded.append(
            JudgeLoss(
                judge_name=name,
                raw_loss=raw_total / n,
                weight=weights[name],
                weighted_loss=weighted_total / n,
            )
        )
    return tuple(folded)


def _average_losses(
    runs: list[dict[str, LossProfile]],
) -> dict[str, LossProfile]:
    """Fold N replicate runs of a board into one per-entry loss map.

    This is the replication primitive: :attr:`ScoringWeights` never sees
    the individual replicates, so EVERY scalar-bearing field must be
    aggregated here or the replicates buy nothing. The rule this function
    holds to is: **a field the scalar or the gate reads is aggregated; a
    field neither reads carries the representative replicate (replicate
    ``0``) and is named below with the reason it may.**

    Aggregated
    ----------
    ``drift_loss``
        Mean across replicates. Reaches the scalar as the ``"drift"``
        component (``drift_weight × drift_loss_mean``).
    ``score``
        Mean across the replicates that HAVE one (:func:`_mean_over_present`);
        ``None`` only when every replicate's is ``None``, so a board with
        no expectations is unchanged. This is the field
        :func:`~zicato.tournament.scoring.entry_score` reads FIRST, hence
        the continuous outcome axis the duel actually turns on — the
        reducer populates it whenever an expectation fired (a bool matcher
        yields ``1.0`` / ``0.0`` just as a float scorer yields its clamped
        value), so it is populated on essentially every scored entry, not
        just float-scorer boards.
    ``metrics``
        Per-key mean over the replicates reporting the key
        (:func:`_mean_metrics`) — the decomposition has to decompose the
        folded ``score`` sitting next to it.
    ``metric_counts``, ``tokens_spent``, ``output_chars``, ``schema_failures``
        Namespace-bearing: they reach the scalar through
        :func:`~zicato.tournament.scoring.aggregate_namespaced_metrics`,
        whose per-namespace values are appended to ``scalar_components``
        and summed into the scalar for any contract with a non-zero
        ``cost:`` / ``output:`` / ``schema:`` weight. ``metric_counts`` is
        the one that matters in production — the reducer always populates
        it, and :meth:`LossProfile.unified_metrics` then reads it in
        preference to synthesising from the three scalars — so it is
        meaned exactly (:func:`_mean_metric_counts`). The three int-typed
        scalars carry the ROUNDED mean: the fields are integer counts by
        contract, and they are consulted only on the synthesised path
        (a profile with no ``metric_counts``) and by display.
    ``per_judge_loss``
        Meaned per judge (:func:`_mean_per_judge_loss`); it is carried onto
        :class:`~zicato.scoring.api.ScalarContext`, so a scalar PLUGIN can
        read it.
    ``pass_fail``
        Strict-majority vote (``None`` preserved when the entry has no
        expectation). NOTE: now that ``score`` is folded, this vote is
        DISPLAY-only for the scalar — :func:`entry_score` returns the
        continuous ``score`` before it can consult ``pass_fail``, and the
        reducer leaves ``score`` unset only on entries that have no
        expectation at all. The vote still drives the binary ``pass_rate``
        and the gate's ``pass_fail`` fallback for score-less aggregates.

    Replicate-0 pass-through, and why each may be
    ---------------------------------------------
    ``run_id``, ``expectation_result``
        Raw provenance of the representative replicate, deliberately NOT
        synthesised: the fold is not a run and has no matcher verdict of
        its own. The AGGREGATED outcome lives in the first-class ``score``
        / ``metrics`` / ``pass_fail`` fields, which are the ones scoring
        and the gate read; ``expectation_result`` stays the untouched raw
        evidence from one replicate.
    ``drift_counts``
        The per-``(kind, severity)`` buckets are NOT scalar-bearing: the
        ``"drift:"`` namespace is explicitly excluded from
        :func:`aggregate_namespaced_metrics` precisely because
        ``drift_loss`` — which IS meaned above — owns the drift axis. The
        buckets are int-typed attribution/display, and the folded
        ``metric_counts`` already carries their meaned ``"drift:"`` mirror.
    ``entry_id``, ``generation_id``, ``epoch_id``, ``match_id``
        Invariant across the replicates of one unit by construction.
    ``runtime_ms``, ``plan_revisions``, ``task_failure_ratio``,
    ``turns_completed``, ``memory_failure_count``, ``context_loss_count``,
    ``adk_session_id``, ``cached`` / ``source_epoch`` / ``source_run``,
    ``scoring_provenance``, ``wall_clock_budget_exceeded``, ``abort_cause``
        Neither the scalar nor the gate reads them. They describe ONE
        execution (its duration, its abort, which cache slot it came from)
        and have no meaningful fold, so they report the representative
        replicate. Consumers that count per-round infra aborts across a
        duel therefore see replicate 0's provenance only — see the
        follow-up note on ``_count_infra_aborted_runs``.

    ``dataclasses.replace`` keeps the profile shape intact, so a field
    added to :class:`LossProfile` later defaults to pass-through and this
    docstring is the place to justify it.
    """
    from dataclasses import replace as _replace  # noqa: PLC0415

    if not runs:
        return {}
    entry_ids = list(runs[0].keys())
    out: dict[str, LossProfile] = {}
    for entry_id in entry_ids:
        profiles = [r[entry_id] for r in runs if entry_id in r]
        if not profiles:
            continue
        n = len(profiles)
        mean_drift = sum(float(p.drift_loss) for p in profiles) / n
        pass_votes = [p.pass_fail for p in profiles if p.pass_fail is not None]
        if pass_votes:
            true_count = sum(1 for v in pass_votes if v)
            majority_pass: bool | None = true_count * 2 > len(pass_votes)
        else:
            majority_pass = None
        out[entry_id] = _replace(
            profiles[0],
            drift_loss=mean_drift,
            pass_fail=majority_pass,
            score=_mean_over_present([p.score for p in profiles]),
            metrics=_mean_metrics(profiles),
            metric_counts=_mean_metric_counts(profiles),
            tokens_spent=round(sum(p.tokens_spent for p in profiles) / n),
            output_chars=round(sum(p.output_chars for p in profiles) / n),
            schema_failures=round(sum(p.schema_failures for p in profiles) / n),
            per_judge_loss=_mean_per_judge_loss(profiles),
        )
    return out


__all__ = [
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
    "read_run_result",
    "run_result_to_payload",
    "unit_result_path",
]
