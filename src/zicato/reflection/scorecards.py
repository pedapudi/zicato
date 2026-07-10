"""Pillar 3/4 aggregation — per-judge confusion matrices + scorecards.

The adjudicator emits one :class:`~zicato.reflection.adjudicator.JudgeAdjudication`
per decision; this module folds them into the per-judge
:class:`JudgeScorecard` the doc's schema pins (BOARD-REFLECTION.md §"judge
audit"). Every metric is honestly named and honestly scoped:

* The confusion matrix is grounded in the ADJUDICATED transcript, not any
  pre-authored label: fired × exhibits = TP, fired × clean = FP, silent ×
  exhibits = FN, silent × clean = TN.
* ``AMBIGUOUS`` decisions are EXCLUDED from precision / recall / f1 / fpr and
  counted separately — an ambiguous pile at or above
  :data:`AMBIGUOUS_PILE_THRESHOLD` is itself a finding (the judge's criterion
  is underspecified), surfaced as ``ambiguous_pile``.
* ``severity_accuracy`` is tracked APART from detection: a judge that fires at
  the wrong severity is a mis-weighting defect, not a detection defect.
* Self-consistency reports BOTH the shipped pairwise ``disagreement_rate``
  (NOT chance-corrected) and a chance-corrected Fleiss ``self_consistency_kappa``
  beside it — neither masquerades as the other (:func:`fleiss_kappa`).
* Cross-judge firing correlation yields ``redundant_with`` (corr ≈ 1, a prune
  candidate) and ``conflicts_with`` (systematic disagreement).
* ``exercised`` reports whether the judge fired at all in this corpus; an
  unexercised judge is UNTESTED, not validated.

Pure over the in-memory adjudications + corpus. Aggregation optionally groups
by fidelity tier so a verbatim scorecard is never silently mixed with a preview
one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from zicato.judge_runtime.reliability import pairwise_disagreement
from zicato.reflection.adjudicator import (
    VERDICT_AMBIGUOUS,
    VERDICT_FN,
    VERDICT_FP,
    VERDICT_TN,
    VERDICT_TP,
    JudgeAdjudication,
)
from zicato.reflection.analysis import pearson
from zicato.reflection.corpus import ObservationRun

#: Fraction of a judge's decisions that may be ``ambiguous`` before the pile is
#: itself flagged — an underspecified criterion the adjudicator (and operator)
#: could not decide on.
AMBIGUOUS_PILE_THRESHOLD: float = 0.2

#: Pearson correlation of two judges' firing vectors at/above which they are
#: ``redundant_with`` each other (one carries no independent signal).
REDUNDANCY_CORR: float = 0.95

#: Correlation at/below which two judges ``conflicts_with`` each other
#: (systematic disagreement — they fire on opposite runs).
CONFLICT_CORR: float = -0.5


@dataclass(frozen=True, slots=True)
class JudgeScorecard:
    """Aggregated audit metrics for ONE judge (the doc's schema)."""

    judge_name: str
    n_decisions: int
    tp: int
    fp: int
    fn: int
    tn: int
    ambiguous: int
    precision: float | None
    recall: float | None
    f1: float | None
    fpr: float | None
    severity_accuracy: float | None
    disagreement_rate: float
    self_consistency_kappa: float | None
    redundant_with: tuple[dict[str, Any], ...]
    conflicts_with: tuple[dict[str, Any], ...]
    exercised: bool
    ambiguous_pile: bool
    fidelity_tiers: tuple[str, ...]
    recommendation: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "judge_name": self.judge_name,
            "n_decisions": self.n_decisions,
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "tn": self.tn,
            "ambiguous": self.ambiguous,
            "precision": self.precision,
            "recall": self.recall,
            "f1": self.f1,
            "fpr": self.fpr,
            "severity_accuracy": self.severity_accuracy,
            "disagreement_rate": self.disagreement_rate,
            "self_consistency_kappa": self.self_consistency_kappa,
            "redundant_with": [dict(r) for r in self.redundant_with],
            "conflicts_with": [dict(c) for c in self.conflicts_with],
            "exercised": self.exercised,
            "ambiguous_pile": self.ambiguous_pile,
            "fidelity_tiers": list(self.fidelity_tiers),
            "recommendation": self.recommendation,
        }


def _safe_div(num: float, den: float) -> float | None:
    """``num/den`` or ``None`` when the denominator is zero (rate undefined)."""
    return num / den if den else None


def fleiss_kappa(items: list[tuple[int, int]]) -> float | None:
    """Chance-corrected Fleiss κ for binary (fired/silent) self-consistency.

    ``items`` is one ``(n_fired, n_raters)`` pair per unit — the replicate
    re-judgements of one ``(candidate, entry)`` unit are the raters, the two
    categories are fired / silent. Fleiss κ requires a UNIFORM rater count, so
    only items sharing the modal ``n_raters`` (≥ 2) are used, and at least two
    such items are needed; otherwise (or when the categories are degenerate, so
    ``1 − P_e == 0``) κ is undefined and ``None`` is returned. Honestly a
    DIFFERENT statistic from the pairwise disagreement rate — reported beside
    it, never in place of it.
    """
    counts: dict[int, int] = {}
    for _fired, n in items:
        if n >= 2:
            counts[n] = counts.get(n, 0) + 1
    if not counts:
        return None
    modal_n = max(counts, key=lambda n: (counts[n], n))
    rows = [(fired, n) for (fired, n) in items if n == modal_n]
    N = len(rows)
    if N < 2:
        return None
    n = modal_n
    # P_i per item over 2 categories: (sum_j n_ij^2 - n) / (n*(n-1)).
    p_i = [((f * f + (n - f) * (n - f)) - n) / (n * (n - 1)) for f, n in rows]
    p_bar = sum(p_i) / N
    total = N * n
    p_fire = sum(f for f, _ in rows) / total
    p_silent = 1.0 - p_fire
    p_e = p_fire * p_fire + p_silent * p_silent
    if 1.0 - p_e == 0.0:
        return None
    return (p_bar - p_e) / (1.0 - p_e)


def _self_consistency(corpus: list[ObservationRun], judge_name: str) -> tuple[float, float | None]:
    """``(worst-unit pairwise disagreement, Fleiss κ)`` for one judge.

    The disagreement rate is the WORST unit's pairwise rate (one flip-flopping
    unit is enough to inject noise); κ is corpus-wide over every unit's
    replicate firings.
    """
    per_unit: dict[tuple[str, str], list[bool]] = {}
    for obs in corpus:
        for decision in obs.judge_decisions:
            if str(decision.get("judge_name", "")) != judge_name:
                continue
            per_unit.setdefault((obs.candidate_id, obs.entry_id), []).append(
                bool(decision.get("fired", False))
            )
    worst = 0.0
    items: list[tuple[int, int]] = []
    for flags in per_unit.values():
        if len(flags) >= 2:
            worst = max(worst, pairwise_disagreement(sum(flags), len(flags)))
            items.append((sum(flags), len(flags)))
    return worst, fleiss_kappa(items)


def _firing_vectors(corpus: list[ObservationRun]) -> dict[str, dict[tuple[str, str, int], int]]:
    """``judge -> {(candidate, entry, replicate) -> 1|0}`` firing map."""
    out: dict[str, dict[tuple[str, str, int], int]] = {}
    for obs in corpus:
        key = (obs.candidate_id, obs.entry_id, obs.replicate)
        for decision in obs.judge_decisions:
            name = str(decision.get("judge_name", ""))
            if not name:
                continue
            out.setdefault(name, {})[key] = 1 if decision.get("fired") else 0
    return out


def _cross_judge(
    vectors: dict[str, dict[tuple[str, str, int], int]], judge_name: str
) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    """``(redundant_with, conflicts_with)`` for one judge vs every other."""
    mine = vectors.get(judge_name, {})
    redundant: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for other, theirs in sorted(vectors.items()):
        if other == judge_name:
            continue
        shared = sorted(set(mine) & set(theirs))
        if len(shared) < 2:
            continue
        corr = pearson([float(mine[k]) for k in shared], [float(theirs[k]) for k in shared])
        if corr >= REDUNDANCY_CORR:
            redundant.append({"judge": other, "corr": corr})
        elif corr <= CONFLICT_CORR:
            conflicts.append({"judge": other, "corr": corr})
    return tuple(redundant), tuple(conflicts)


def _recommend(
    *,
    exercised: bool,
    precision: float | None,
    recall: float | None,
    redundant_with: tuple[dict[str, Any], ...],
    ambiguous_pile: bool,
) -> str:
    """A one-line operator recommendation from the folded metrics."""
    if not exercised:
        return "untested — never fired in this corpus; add an entry that exercises its kind"
    notes: list[str] = []
    if ambiguous_pile:
        notes.append("large ambiguous pile — criterion underspecified; tighten the rubric")
    if redundant_with:
        notes.append(
            "redundant with " + ", ".join(r["judge"] for r in redundant_with) + " — prune for cost"
        )
    if precision is not None and precision < 0.5:
        notes.append("false-fire heavy — down-weight or tighten")
    if recall is not None and recall < 0.5:
        notes.append("misses real failures — broaden or add coverage")
    return "; ".join(notes) if notes else "healthy"


def build_scorecard(
    *,
    judge_name: str,
    adjudications: list[JudgeAdjudication],
    corpus: list[ObservationRun],
    vectors: dict[str, dict[tuple[str, str, int], int]],
) -> JudgeScorecard:
    """Fold one judge's decisions into a :class:`JudgeScorecard`."""
    mine = [a for a in adjudications if a.judge_name == judge_name]
    tp = sum(1 for a in mine if a.verdict == VERDICT_TP)
    fp = sum(1 for a in mine if a.verdict == VERDICT_FP)
    fn = sum(1 for a in mine if a.verdict == VERDICT_FN)
    tn = sum(1 for a in mine if a.verdict == VERDICT_TN)
    ambiguous = sum(1 for a in mine if a.verdict == VERDICT_AMBIGUOUS)
    n_decisions = len(mine)

    precision = _safe_div(tp, tp + fp)
    recall = _safe_div(tp, tp + fn)
    fpr = _safe_div(fp, fp + tn)
    f1 = _safe_div(2 * tp, 2 * tp + fp + fn)

    sev_scored = [a.severity_match for a in mine if a.severity_match is not None]
    severity_accuracy = sum(1 for m in sev_scored if m) / len(sev_scored) if sev_scored else None

    disagreement_rate, kappa = _self_consistency(corpus, judge_name)
    redundant_with, conflicts_with = _cross_judge(vectors, judge_name)
    exercised = any(v == 1 for v in vectors.get(judge_name, {}).values())
    rated = n_decisions - ambiguous
    ambiguous_pile = n_decisions > 0 and (ambiguous / n_decisions) >= AMBIGUOUS_PILE_THRESHOLD

    fidelity_tiers = tuple(sorted({a.fidelity for a in mine}))
    recommendation = _recommend(
        exercised=exercised,
        precision=precision,
        recall=recall,
        redundant_with=redundant_with,
        ambiguous_pile=ambiguous_pile,
    )
    # rated is surfaced implicitly via n_decisions - ambiguous; kept for clarity.
    _ = rated
    return JudgeScorecard(
        judge_name=judge_name,
        n_decisions=n_decisions,
        tp=tp,
        fp=fp,
        fn=fn,
        tn=tn,
        ambiguous=ambiguous,
        precision=precision,
        recall=recall,
        f1=f1,
        fpr=fpr,
        severity_accuracy=severity_accuracy,
        disagreement_rate=disagreement_rate,
        self_consistency_kappa=kappa,
        redundant_with=redundant_with,
        conflicts_with=conflicts_with,
        exercised=exercised,
        ambiguous_pile=ambiguous_pile,
        fidelity_tiers=fidelity_tiers,
        recommendation=recommendation,
    )


def build_scorecards(
    *,
    adjudications: list[JudgeAdjudication],
    corpus: list[ObservationRun],
) -> list[JudgeScorecard]:
    """One :class:`JudgeScorecard` per judge that appears in the adjudications.

    Judges are swept in sorted name order. The confusion arithmetic is exact by
    construction: ``tp + fp + fn + tn + ambiguous == n_decisions`` for every
    card (a property the tests pin), with the ambiguous pile excluded from the
    rates.
    """
    vectors = _firing_vectors(corpus)
    names = sorted({a.judge_name for a in adjudications})
    return [
        build_scorecard(
            judge_name=name, adjudications=adjudications, corpus=corpus, vectors=vectors
        )
        for name in names
    ]


def build_scorecards_by_fidelity(
    *,
    adjudications: list[JudgeAdjudication],
    corpus: list[ObservationRun],
) -> dict[str, list[JudgeScorecard]]:
    """Per-fidelity-tier scorecards — tiers are NEVER silently mixed.

    Groups both the adjudications and the corpus by fidelity tier and builds an
    independent scorecard set per tier, so a verbatim audit and a preview audit
    are reported side by side rather than averaged together.
    """
    tiers = sorted({a.fidelity for a in adjudications})
    out: dict[str, list[JudgeScorecard]] = {}
    for tier in tiers:
        tier_adj = [a for a in adjudications if a.fidelity == tier]
        tier_corpus = [o for o in corpus if o.fidelity == tier]
        out[tier] = build_scorecards(adjudications=tier_adj, corpus=tier_corpus)
    return out


__all__ = [
    "AMBIGUOUS_PILE_THRESHOLD",
    "CONFLICT_CORR",
    "REDUNDANCY_CORR",
    "JudgeScorecard",
    "build_scorecard",
    "build_scorecards",
    "build_scorecards_by_fidelity",
    "fleiss_kappa",
]
