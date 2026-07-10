"""Pillars 1-2 — reliability and discrimination over the observation corpus.

Every function here is **PURE**: it reads the in-memory corpus
(:class:`~zicato.reflection.corpus.ObservationRun` list) plus injected records
(the persisted noise floor, the board's declared judges/kinds, the epoch's
experiments) and returns a plain dict. No I/O, no LLM, no wall-clock — so each
is a known-answer unit test away from trusted.

Pillar 1 — reliability (repetition, no adjudication)
----------------------------------------------------
* :func:`noise_floor_summary` — CONSUMES the noise floor already persisted on
  the epoch record (a ``fresh`` re-measure is a CLI concern, wired later) and
  adds the corpus' own per-candidate scalar SD.
* :func:`decision_flip_probability` — the headline number: a **seeded
  bootstrap** (``B=1000``, RNG seeded from the plan's ``reflection_id``) that
  resamples per-unit replicate scalars and pushes each resample through the
  pure gate-margin decision ``child > parent - promote_margin``, reporting
  ``P(flip)``. The run-the-tournament-twice form is endpoint-gated validation
  of exactly this.
* :func:`judge_self_consistency` — folds the corpus' verbatim judge firings
  into the :class:`~zicato.judge_runtime.reliability.JudgeReliability` shape and
  feeds the EXISTING :func:`zicato.health.diagnostics.detect_noisy_judge`
  unchanged (one threshold, one finding shape — no parallel taxonomy).
* :func:`placebo_outcomes` — surfaces
  :func:`zicato.health.diagnostics.detect_placebo_promoted` (cite the existing
  gate-discrimination signal, do not reinvent it).

Pillar 2 — discrimination / power (a spread of candidates)
----------------------------------------------------------
* :func:`entry_differentiation` — does each entry's score MOVE across
  candidates? A flat entry is information-free.
* :func:`entry_candidate_matrix` + :func:`redundancy_clusters` — the
  entry×candidate matrix and a greedy Pearson redundancy clustering
  (hand-rolled Pearson, NO scipy).
* :func:`power_analysis` — the closed-form minimum detectable Δscalar from σ,
  K, n at a chosen confidence.
* :func:`coverage` — exercised drift-kinds/judges vs what the board watches; a
  judge that never fired is flagged ``untested`` (cannot be validated here).

Every returned dict carries a ``fidelity_tiers`` key naming the source tiers it
drew from — tiers are surfaced, never silently mixed.
"""

from __future__ import annotations

import hashlib
import math
import random
import statistics
from typing import Any

from zicato.reflection.corpus import (
    FIDELITY_PREVIEW,
    FIDELITY_RESULT,
    FIDELITY_VERBATIM,
    ObservationRun,
)

#: The default bootstrap resample count for the decision-flip estimate.
DEFAULT_BOOTSTRAP_B: int = 1000

#: The default Pearson redundancy threshold — entries whose score vectors
#: correlate at or above this cluster as mutually redundant (prune for cost).
DEFAULT_REDUNDANCY_THRESHOLD: float = 0.95

_TIER_ORDER = {FIDELITY_VERBATIM: 0, FIDELITY_RESULT: 1, FIDELITY_PREVIEW: 2}


# ---------------------------------------------------------------------------
# Shared corpus reshaping helpers
# ---------------------------------------------------------------------------


def _fidelity_tiers(observations: list[ObservationRun]) -> list[str]:
    """Distinct fidelity tiers among ``observations``, strongest first."""
    tiers = {obs.fidelity for obs in observations}
    return sorted(tiers, key=lambda t: _TIER_ORDER.get(t, 99))


def _unit_scalars(
    observations: list[ObservationRun],
) -> dict[tuple[str, str], list[float]]:
    """Map ``(candidate, entry) -> [replicate scalars]``."""
    out: dict[tuple[str, str], list[float]] = {}
    for obs in observations:
        out.setdefault((obs.candidate_id, obs.entry_id), []).append(obs.scalar)
    return out


def _candidates(observations: list[ObservationRun]) -> list[str]:
    return sorted({obs.candidate_id for obs in observations})


def _entries(observations: list[ObservationRun]) -> list[str]:
    return sorted({obs.entry_id for obs in observations})


def _population_sd(values: list[float]) -> float:
    """Population SD (0.0 for fewer than two values)."""
    if len(values) < 2:
        return 0.0
    return statistics.pstdev(values)


# ---------------------------------------------------------------------------
# Pillar 1 — reliability
# ---------------------------------------------------------------------------


def noise_floor_summary(
    *,
    corpus: list[ObservationRun],
    epoch_noise_floor: dict[str, Any] | None,
    epoch_preflight: dict[str, Any] | None = None,
    fresh: bool = False,
) -> dict[str, Any]:
    """Reliability summary: the CONSUMED floor + per-candidate scalar SD.

    The noise floor is read from the epoch record's persisted
    :meth:`zicato.tournament.calibration.NoiseFloor.to_json` dict, never
    re-measured here — reflection reuses the calibration budget the loop
    already spent. ``fresh`` is a documented pass-through flag: the CLI phase
    wires ``--fresh`` to re-measure deliberately; this pure function only
    RECORDS the operator's intent (``fresh=True`` means "the caller re-measured
    and passed a fresh floor in"), it never performs the measurement.

    Per-candidate scalar SD is the corpus' own reliability read: for each
    candidate, its replicate-level scalars (mean over the candidate's entries
    per replicate index) and their population SD.
    """
    floor_max_abs = None
    floor_runs = None
    if isinstance(epoch_noise_floor, dict):
        try:
            floor_max_abs = float(epoch_noise_floor.get("max_abs_delta", 0.0))
        except (TypeError, ValueError):
            floor_max_abs = None
        floor_runs = epoch_noise_floor.get("runs")

    per_candidate: dict[str, dict[str, Any]] = {}
    for candidate in _candidates(corpus):
        obs = [o for o in corpus if o.candidate_id == candidate]
        # Replicate-level candidate scalar = mean over the candidate's entries
        # sharing a replicate index.
        by_replicate: dict[int, list[float]] = {}
        for o in obs:
            by_replicate.setdefault(o.replicate, []).append(o.scalar)
        draw_scalars = [statistics.fmean(v) for v in by_replicate.values() if v]
        per_candidate[candidate] = {
            "scalar_sd": _population_sd(draw_scalars),
            "n_draws": len(draw_scalars),
        }

    return {
        "consumed": not fresh,
        "fresh": fresh,
        "noise_floor_max_abs_delta": floor_max_abs,
        "noise_floor_runs": floor_runs,
        "preflight_verdict": (
            epoch_preflight.get("verdict") if isinstance(epoch_preflight, dict) else None
        ),
        "per_candidate_scalar_sd": per_candidate,
        "fidelity_tiers": _fidelity_tiers(corpus),
    }


def _bootstrap_seed(reflection_id: str) -> int:
    """A stable 32-bit RNG seed from the plan's ``reflection_id``."""
    digest = hashlib.sha256(reflection_id.encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


def _candidate_scalar(
    unit_scalars: dict[tuple[str, str], list[float]],
    candidate: str,
    entries: list[str],
    picker: Any,
) -> float:
    """Candidate scalar = mean over its entries of one picked replicate scalar.

    ``picker`` is either ``statistics.fmean`` (the point estimate over all a
    unit's replicates) or an ``rng.choice`` (one bootstrap resample).
    """
    per_entry: list[float] = []
    for entry in entries:
        scalars = unit_scalars.get((candidate, entry))
        if scalars:
            per_entry.append(picker(scalars))
    return statistics.fmean(per_entry) if per_entry else 0.0


def decision_flip_probability(
    *,
    corpus: list[ObservationRun],
    reflection_id: str,
    parent_id: str,
    child_id: str,
    promote_margin: float,
    entries: list[str] | None = None,
    b: int = DEFAULT_BOOTSTRAP_B,
) -> dict[str, Any]:
    """Seeded-bootstrap probability the promote decision flips under re-draw.

    The pure gate-margin decision is ``child_scalar > parent_scalar -
    promote_margin`` (higher scalar = better, per the plan). The point-estimate
    decision uses each unit's mean replicate scalar; each of ``b`` bootstrap
    resamples draws ONE replicate scalar per unit (RNG seeded deterministically
    from ``reflection_id``) and re-decides. ``P(flip)`` is the fraction of
    resamples whose decision differs from the point estimate — the headline
    decision-level reliability number.

    Known-answer behavior: a margin far larger than the scalar spread ⇒
    ``p_flip ≈ 0``; a margin below the spread ⇒ a materially positive
    ``p_flip``; the same ``reflection_id`` ⇒ an identical ``p_flip``.
    """
    obs = [o for o in corpus if o.candidate_id in (parent_id, child_id)]
    entry_list = entries if entries is not None else _entries(obs)
    unit_scalars = _unit_scalars(obs)

    def _decide(picker: Any) -> bool:
        parent = _candidate_scalar(unit_scalars, parent_id, entry_list, picker)
        child = _candidate_scalar(unit_scalars, child_id, entry_list, picker)
        return child > parent - promote_margin

    base_decision = _decide(statistics.fmean)
    rng = random.Random(_bootstrap_seed(reflection_id))
    flips = 0
    for _ in range(b):
        if _decide(rng.choice) != base_decision:
            flips += 1
    return {
        "p_flip": flips / b if b else 0.0,
        "base_decision": "promote" if base_decision else "reject",
        "b": b,
        "parent_id": parent_id,
        "child_id": child_id,
        "promote_margin": promote_margin,
        "fidelity_tiers": _fidelity_tiers(obs),
    }


def judge_self_consistency(*, corpus: list[ObservationRun]) -> dict[str, Any]:
    """Feed the corpus' verbatim judge firings to ``detect_noisy_judge``.

    For each judge, the replicate re-judgements of each ``(candidate, entry)``
    unit are a test-retest over near-identical input; the judge's reported
    disagreement is the WORST such unit (one flip-flopping unit is enough to
    inject noise), computed by the shipped
    :func:`zicato.judge_runtime.reliability.pairwise_disagreement`. The result
    is packed into :class:`~zicato.judge_runtime.reliability.JudgeReliability`
    records and passed UNCHANGED to
    :func:`zicato.health.diagnostics.detect_noisy_judge`.
    """
    from zicato.health.diagnostics import detect_noisy_judge  # noqa: PLC0415
    from zicato.judge_runtime.reliability import (  # noqa: PLC0415
        JudgeReliability,
        pairwise_disagreement,
    )

    # (judge_name, candidate, entry) -> [fired flags across replicates]
    per_unit: dict[tuple[str, str, str], list[bool]] = {}
    for obs in corpus:
        for decision in obs.judge_decisions:
            name = str(decision.get("judge_name", ""))
            if not name:
                continue
            per_unit.setdefault((name, obs.candidate_id, obs.entry_id), []).append(
                bool(decision.get("fired", False))
            )

    per_judge_flags: dict[str, list[bool]] = {}
    per_judge_worst: dict[str, float] = {}
    for (name, _cand, _entry), flags in per_unit.items():
        per_judge_flags.setdefault(name, []).extend(flags)
        if len(flags) >= 2:
            rate = pairwise_disagreement(sum(flags), len(flags))
            per_judge_worst[name] = max(per_judge_worst.get(name, 0.0), rate)

    reliabilities: list[JudgeReliability] = []
    for name in sorted(per_judge_flags):
        flags = per_judge_flags[name]
        reliabilities.append(
            JudgeReliability(
                judge_name=name,
                k=len(flags),
                fired=sum(flags),
                verdicts=tuple(flags),
                disagreement_rate=per_judge_worst.get(name, 0.0),
                details=(),
            )
        )

    findings = detect_noisy_judge(reliabilities)
    return {
        "judges": [rel.to_json() for rel in reliabilities],
        "noisy_judge_findings": [
            {"code": f.code, "severity": f.severity, "summary": f.summary, "detail": f.detail}
            for f in findings
        ],
        "fidelity_tiers": _fidelity_tiers([o for o in corpus if o.judge_decisions]),
    }


def placebo_outcomes(*, corpus: list[ObservationRun], experiments: list[Any]) -> dict[str, Any]:
    """Surface the existing placebo gate-discrimination signal (cite, not reinvent).

    Delegates to :func:`zicato.health.diagnostics.detect_placebo_promoted` — a
    promoted random-baseline is the loudest possible evidence the gate is
    promoting noise. Reflection only re-surfaces it beside the reliability
    pillar; it does not re-derive the placebo arm.
    """
    from zicato.health.diagnostics import detect_placebo_promoted  # noqa: PLC0415

    findings = detect_placebo_promoted(experiments)
    return {
        "placebo_promoted_findings": [
            {"code": f.code, "severity": f.severity, "summary": f.summary, "detail": f.detail}
            for f in findings
        ],
        "fidelity_tiers": _fidelity_tiers(corpus),
    }


# ---------------------------------------------------------------------------
# Pillar 2 — discrimination / power
# ---------------------------------------------------------------------------


def _mean_matrix(
    observations: list[ObservationRun],
) -> tuple[list[str], list[str], dict[tuple[str, str], float]]:
    """``(entries, candidates, {(entry, candidate) -> mean scalar})``."""
    entries = _entries(observations)
    candidates = _candidates(observations)
    cell: dict[tuple[str, str], list[float]] = {}
    for obs in observations:
        cell.setdefault((obs.entry_id, obs.candidate_id), []).append(obs.scalar)
    means = {key: statistics.fmean(vals) for key, vals in cell.items() if vals}
    return entries, candidates, means


def entry_differentiation(*, corpus: list[ObservationRun], epsilon: float = 0.0) -> dict[str, Any]:
    """Per-entry: does the entry's mean score MOVE across candidates?

    A flat entry (spread ``<= epsilon`` across candidates) is information-free
    — it generalizes ``detect_non_differentiating_entry`` to the reflection
    corpus. Entries observed under fewer than two candidates cannot yet be
    judged (``differentiates`` is ``None``).
    """
    entries, candidates, means = _mean_matrix(corpus)
    out: list[dict[str, Any]] = []
    for entry in entries:
        row = [means[(entry, c)] for c in candidates if (entry, c) in means]
        if len(row) < 2:
            differentiates: bool | None = None
            spread = 0.0
        else:
            spread = max(row) - min(row)
            differentiates = spread > epsilon
        out.append(
            {
                "entry_id": entry,
                "differentiates": differentiates,
                "spread": spread,
                "n_candidates": len(row),
            }
        )
    return {"entries": out, "fidelity_tiers": _fidelity_tiers(corpus)}


def entry_candidate_matrix(*, corpus: list[ObservationRun]) -> dict[str, Any]:
    """The entry×candidate mean-scalar matrix (row-major, sorted axes)."""
    entries, candidates, means = _mean_matrix(corpus)
    matrix = [[means.get((entry, cand)) for cand in candidates] for entry in entries]
    return {
        "entries": entries,
        "candidates": candidates,
        "matrix": matrix,
        "fidelity_tiers": _fidelity_tiers(corpus),
    }


def pearson(x: list[float], y: list[float]) -> float:
    """Pearson correlation of two equal-length vectors (hand-rolled, no scipy).

    Zero-variance handling: two identical vectors (including two identical
    constants — perfectly redundant entries) return ``1.0``; a single
    zero-variance vector against a varying one returns ``0.0`` (undefined
    correlation ⇒ "not redundant").
    """
    if len(x) != len(y) or len(x) < 2:
        return 0.0
    mean_x = statistics.fmean(x)
    mean_y = statistics.fmean(y)
    dx = [xi - mean_x for xi in x]
    dy = [yi - mean_y for yi in y]
    num = sum(a * b for a, b in zip(dx, dy, strict=True))
    den = math.sqrt(sum(a * a for a in dx) * sum(b * b for b in dy))
    if den == 0.0:
        return 1.0 if x == y else 0.0
    return num / den


def redundancy_clusters(
    *,
    corpus: list[ObservationRun],
    threshold: float = DEFAULT_REDUNDANCY_THRESHOLD,
) -> dict[str, Any]:
    """Greedy Pearson redundancy clustering over the entry×candidate matrix.

    Each entry is a vector over the (shared) candidate axis of its mean
    scalars; two entries whose vectors correlate at or above ``threshold`` are
    mutually redundant (one is a prune candidate for cost). Greedy: entries are
    swept in sorted order, each unclustered entry seeds a cluster that absorbs
    every later unclustered entry correlating with the seed at/above the
    threshold. Singletons are their own cluster. Entries observed under fewer
    than two shared candidates are returned uncorrelatable (their own cluster).
    """
    entries, candidates, means = _mean_matrix(corpus)
    # Only candidates present for an entry can vector it; use the full sorted
    # candidate axis and require every entry to have all of them for a valid
    # pairwise correlation (missing cells ⇒ the entries are not comparable).
    vectors: dict[str, list[float]] = {}
    for entry in entries:
        row = [means.get((entry, cand)) for cand in candidates]
        if all(v is not None for v in row) and len(row) >= 2:
            vectors[entry] = [float(v) for v in row]  # type: ignore[arg-type]

    clustered: set[str] = set()
    clusters: list[list[str]] = []
    for seed in entries:
        if seed in clustered:
            continue
        cluster = [seed]
        clustered.add(seed)
        if seed in vectors:
            for other in entries:
                if other in clustered or other not in vectors:
                    continue
                if pearson(vectors[seed], vectors[other]) >= threshold:
                    cluster.append(other)
                    clustered.add(other)
        clusters.append(cluster)

    redundant = [c for c in clusters if len(c) > 1]
    return {
        "clusters": clusters,
        "redundant_clusters": redundant,
        "threshold": threshold,
        "fidelity_tiers": _fidelity_tiers(corpus),
    }


def _z_for_confidence(confidence: float) -> float:
    """Two-sided z multiplier for a confidence level (closed form, no scipy)."""
    tail = 1.0 - (1.0 - confidence) / 2.0
    return statistics.NormalDist().inv_cdf(tail)


def power_analysis(
    *,
    sigma: float,
    k: int,
    n: int,
    confidence: float = 0.95,
) -> dict[str, Any]:
    """Closed-form minimum detectable Δscalar from σ, K replicates, n entries.

    Models a candidate's mean scalar as an average over ``n`` entries each with
    per-unit SD ``sigma`` and ``k`` replicates; the standard error of the
    child−parent difference of two such means is ``sigma * sqrt(2 / (k * n))``,
    so the two-sided minimum detectable difference at ``confidence`` is
    ``z * sigma * sqrt(2 / (k * n))``. Degenerate ``k <= 0`` / ``n <= 0`` ⇒
    ``inf`` (nothing is detectable with no data).
    """
    z = _z_for_confidence(confidence)
    if k <= 0 or n <= 0:
        mde = math.inf
    else:
        mde = z * float(sigma) * math.sqrt(2.0 / (k * n))
    return {
        "min_detectable_delta": mde,
        "sigma": float(sigma),
        "k": k,
        "n": n,
        "confidence": confidence,
        "z": z,
    }


def coverage(
    *,
    corpus: list[ObservationRun],
    board_kinds: list[str] | tuple[str, ...],
    board_judges: list[str] | tuple[str, ...],
) -> dict[str, Any]:
    """Exercised drift-kinds/judges vs what the board watches.

    A drift kind the board watches but no run ever exercised is uncovered; a
    judge that never fired across the corpus is **untested** — it cannot be
    validated by this corpus, only reported. Exercised = the judge fired at
    least once (a verbatim firing) or authored at least one ``custom:<judge>``
    drift event.
    """
    exercised_kinds: set[str] = set()
    exercised_judges: set[str] = set()
    for obs in corpus:
        for event in obs.drift_events:
            kind = str(event.get("kind", ""))
            if int(event.get("count", 0)) > 0:
                exercised_kinds.add(kind)
            judge_name = str(event.get("judge_name", ""))
            if judge_name and int(event.get("count", 0)) > 0:
                exercised_judges.add(judge_name)
        for decision in obs.judge_decisions:
            if decision.get("fired"):
                exercised_judges.add(str(decision.get("judge_name", "")))

    watched_kinds = set(board_kinds)
    uncovered_kinds = sorted(watched_kinds - exercised_kinds)
    judges = [
        {
            "judge_name": name,
            "exercised": name in exercised_judges,
            "untested": name not in exercised_judges,
        }
        for name in sorted(board_judges)
    ]
    return {
        "exercised_kinds": sorted(exercised_kinds),
        "watched_kinds": sorted(watched_kinds),
        "uncovered_kinds": uncovered_kinds,
        "judges": judges,
        "untested_judges": [j["judge_name"] for j in judges if j["untested"]],
        "fidelity_tiers": _fidelity_tiers(corpus),
    }


__all__ = [
    "DEFAULT_BOOTSTRAP_B",
    "DEFAULT_REDUNDANCY_THRESHOLD",
    "coverage",
    "decision_flip_probability",
    "entry_candidate_matrix",
    "entry_differentiation",
    "judge_self_consistency",
    "noise_floor_summary",
    "pearson",
    "placebo_outcomes",
    "power_analysis",
    "redundancy_clusters",
]
