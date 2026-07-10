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


def _bootstrap_seed(reflection_id: str, parent_id: str, child_id: str) -> int:
    """A stable 32-bit RNG seed folding the reflection + the specific pair.

    Folding ``parent_id`` / ``child_id`` in (N1) makes every pair draw an
    INDEPENDENT resample stream — two pairs adjudicated under the same
    reflection no longer share a bootstrap trajectory, so their ``p_flip``
    estimates are statistically independent rather than coupled through a
    common seed.
    """
    digest = hashlib.sha256(f"{reflection_id}|{parent_id}|{child_id}".encode()).hexdigest()
    return int(digest[:8], 16)


def _candidate_scalar(
    unit_scalars: dict[tuple[str, str], list[float]],
    candidate: str,
    entries: list[str],
    resampler: Any,
) -> float:
    """Candidate scalar = mean over its entries of a per-unit resampled scalar.

    ``resampler`` maps a unit's replicate-scalar list to a single scalar: the
    point estimate uses :func:`statistics.fmean` (the mean of all K replicates);
    a bootstrap resample uses a per-unit estimator (see
    :func:`decision_flip_probability`).
    """
    per_entry: list[float] = []
    for entry in entries:
        scalars = unit_scalars.get((candidate, entry))
        if scalars:
            per_entry.append(resampler(scalars))
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
    resample: str = "k_mean",
) -> dict[str, Any]:
    """Seeded-bootstrap probability the promote decision flips under re-draw.

    The pure gate-margin decision is ``child_scalar > parent_scalar -
    promote_margin`` (higher scalar = better, per the plan). The point-estimate
    decision uses each unit's MEAN replicate scalar (the base estimator is the
    mean of K).

    Each of ``b`` bootstrap resamples rebuilds every unit's scalar by the
    ``resample`` estimator and re-decides; ``P(flip)`` is the fraction of
    resamples whose decision differs from the point estimate.

    ``resample`` (S1):

    * ``"k_mean"`` (default) — draw ``K`` replicate scalars WITH REPLACEMENT and
      AVERAGE them, exactly matching the base mean-of-K estimator. This is the
      statistically honest bootstrap: it reproduces the sampling distribution of
      the quantity the gate actually compares (a mean of K), so its variance —
      and therefore ``p_flip`` — is calibrated, not inflated.
    * ``"single"`` — draw ONE replicate scalar per unit (the old behavior). It
      resamples the distribution of a SINGLE draw, whose variance is ``√K``×
      larger than the mean-of-K the gate uses, so it systematically OVERSTATES
      ``p_flip``. Retained only as the higher-variance reference the tests pin
      the default against.

    Returns ``p_flip=None`` with a ``reason`` (S2) when the bootstrap is
    undefined: either candidate has NO observations, or ANY contributing
    ``(candidate, entry)`` unit has fewer than two replicates (a single draw
    carries no resample spread — a fabricated ``0.0`` would falsely read as
    "perfectly reliable").

    Known-answer behavior: a margin far larger than the scalar spread ⇒
    ``p_flip == 0``; a margin below the spread ⇒ a materially positive
    ``p_flip`` that decreases as the margin grows; the same
    ``(reflection_id, parent, child)`` ⇒ an identical ``p_flip``.
    """
    obs = [o for o in corpus if o.candidate_id in (parent_id, child_id)]
    entry_list = entries if entries is not None else _entries(obs)
    unit_scalars = _unit_scalars(obs)

    def _null(reason: str) -> dict[str, Any]:
        return {
            "p_flip": None,
            "reason": reason,
            "base_decision": None,
            "b": b,
            "parent_id": parent_id,
            "child_id": child_id,
            "promote_margin": promote_margin,
            "fidelity_tiers": _fidelity_tiers(obs),
        }

    # S2 degeneracy guards — before any bootstrap.
    contributing: list[list[float]] = []
    for candidate in (parent_id, child_id):
        present = [
            unit_scalars[(candidate, entry)]
            for entry in entry_list
            if unit_scalars.get((candidate, entry))
        ]
        if not present:
            return _null(f"candidate {candidate!r} has no observations for these entries")
        contributing.extend(present)
    if any(len(scalars) < 2 for scalars in contributing):
        return _null(
            "a contributing (candidate, entry) unit has fewer than two replicates; "
            "the decision-flip bootstrap needs ≥2 draws per unit to resample"
        )

    def _decide(resampler: Any) -> bool:
        parent = _candidate_scalar(unit_scalars, parent_id, entry_list, resampler)
        child = _candidate_scalar(unit_scalars, child_id, entry_list, resampler)
        return child > parent - promote_margin

    base_decision = _decide(statistics.fmean)
    rng = random.Random(_bootstrap_seed(reflection_id, parent_id, child_id))

    if resample == "single":
        resampler: Any = rng.choice
    else:

        def resampler(scalars: list[float]) -> float:  # k-with-replacement mean
            return statistics.fmean([rng.choice(scalars) for _ in range(len(scalars))])

    flips = 0
    for _ in range(b):
        if _decide(resampler) != base_decision:
            flips += 1
    return {
        "p_flip": flips / b if b else 0.0,
        "reason": None,
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
    unit are a test-retest over near-identical input. The disagreement rate fed
    to the detector is POOLED (S3): total disagreeing verdict pairs summed over
    all units, divided by the total unordered pairs summed over all units — the
    natural per-judge dispersion, with the record's ``k`` / ``fired`` totals as
    the SAME pooled totals. (The old worst-unit maximum let a single 2-draw flip
    read as a 100%-noisy judge; it is retained as a secondary
    ``worst_unit_disagreement`` diagnostic per judge, NOT fed to the detector.)
    The pooled rate is packed into
    :class:`~zicato.judge_runtime.reliability.JudgeReliability` and passed
    UNCHANGED to :func:`zicato.health.diagnostics.detect_noisy_judge`.
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
    per_judge_disagree_pairs: dict[str, float] = {}
    per_judge_total_pairs: dict[str, float] = {}
    per_judge_worst: dict[str, float] = {}
    for (name, _cand, _entry), flags in per_unit.items():
        per_judge_flags.setdefault(name, []).extend(flags)
        k = len(flags)
        if k >= 2:
            fired = sum(flags)
            per_judge_disagree_pairs[name] = per_judge_disagree_pairs.get(name, 0.0) + fired * (
                k - fired
            )
            per_judge_total_pairs[name] = per_judge_total_pairs.get(name, 0.0) + k * (k - 1) / 2.0
            per_judge_worst[name] = max(
                per_judge_worst.get(name, 0.0), pairwise_disagreement(fired, k)
            )

    reliabilities: list[JudgeReliability] = []
    for name in sorted(per_judge_flags):
        flags = per_judge_flags[name]
        total_pairs = per_judge_total_pairs.get(name, 0.0)
        pooled = per_judge_disagree_pairs.get(name, 0.0) / total_pairs if total_pairs else 0.0
        reliabilities.append(
            JudgeReliability(
                judge_name=name,
                k=len(flags),
                fired=sum(flags),
                verdicts=tuple(flags),
                disagreement_rate=pooled,
                details=(),
            )
        )

    findings = detect_noisy_judge(reliabilities)
    judges_out: list[dict[str, Any]] = []
    for rel in reliabilities:
        record = rel.to_json()
        record["worst_unit_disagreement"] = per_judge_worst.get(rel.judge_name, 0.0)
        judges_out.append(record)
    return {
        "judges": judges_out,
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


def sigma_from_noise_floor(floor: dict[str, Any] | None) -> float | None:
    """The PER-UNIT scalar σ for :func:`power_analysis`, from a persisted floor.

    ⚠ ``power_analysis``'s ``sigma`` is the PER-UNIT scalar standard deviation
    (the dispersion of ONE unit's replicate scalars). Neither field a persisted
    :meth:`zicato.tournament.calibration.NoiseFloor.to_json` carries is that σ:

    * ``delta_std`` is ALREADY the ``√2``-scaled SD of the child−parent
      DIFFERENCE (``sqrt(2) * pstdev(scalars)``) — feeding it in as ``sigma``
      double-counts the ``√2`` the formula itself applies.
    * ``max_abs_delta`` is a RANGE (``max(scalars) - min(scalars)``), not a
      standard deviation at all — feeding it in wildly overstates σ.

    This helper computes the honest per-unit σ = ``pstdev(floor["scalars"])``
    from the floor's raw A/A draw scalars, returning ``None`` when the floor is
    absent or carries fewer than two scalars (σ undefined).
    """
    if not isinstance(floor, dict):
        return None
    scalars = floor.get("scalars")
    if not isinstance(scalars, list | tuple):
        return None
    values = [float(x) for x in scalars if isinstance(x, int | float) and not isinstance(x, bool)]
    if len(values) < 2:
        return None
    return statistics.pstdev(values)


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

    ⚠ ``sigma`` MUST be the PER-UNIT scalar SD. Do NOT pass a noise floor's
    ``delta_std`` (already ``√2``-scaled — the formula re-applies the ``√2``) or
    its ``max_abs_delta`` (a range, not an SD); both overstate σ and inflate the
    MDE. Derive the correct σ with :func:`sigma_from_noise_floor`.
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
    "sigma_from_noise_floor",
]
