"""Practice review — the narrative layer above the four pillars.

The pillars answer *what the numbers say* about one contract; the practice
review answers *what you should change about how you evaluate*. It diagnoses
(anti-)best practices — sound authoring as loudly as unsound authoring — over
the contract, the operating history, and (when a ``reflect run`` produced them)
the reflection artifacts. **Zero LLM calls**: every input is a pure read (the
free passive tier). Recommend-only: a mechanically-fixable check carries a
``proposed_op`` naming a REAL builder op, validated against that op's signature
at emit time exactly as :mod:`zicato.reflection.findings` validates its own.

The design of record is ``docs/design/BOARD-REFLECTION.md`` §"Practice review".

Composition, not re-derivation
------------------------------
Where a loop-health detector or an analysis function already owns a signal, the
check **calls it and translates its finding** — it never re-implements the
arithmetic, so a threshold change in the detector moves the practice verdict
with it (no drift):

* :func:`statistical_power` consumes
  :func:`zicato.reflection.analysis.power_analysis` (fed by
  :func:`~zicato.reflection.analysis.sigma_from_noise_floor`);
* :func:`placebo_outcomes` composes
  :func:`zicato.health.diagnostics.detect_placebo_promoted`;
* :func:`generalization_trend` composes
  :func:`zicato.health.diagnostics.detect_generalization_gap`;
* :func:`promotion_hygiene` composes
  :func:`zicato.health.diagnostics.detect_margin_below_noise_floor`.

The verdict vocabulary is ``sound`` (an affirmation — reported, never
suppressed) / ``attend`` (a soft deficiency) / ``unsound`` (an anti-practice
the doctrine names as wrong) / ``unmeasured`` (inputs absent — reported with the
missing input, never a fabricated verdict).
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Verdict vocabulary
# ---------------------------------------------------------------------------

VERDICT_SOUND: str = "sound"
VERDICT_ATTEND: str = "attend"
VERDICT_UNSOUND: str = "unsound"
VERDICT_UNMEASURED: str = "unmeasured"

# ---------------------------------------------------------------------------
# Thresholds (module constants with rationale)
# ---------------------------------------------------------------------------

#: Outcome-oracle kinds that grade a run with real structure — a board that
#: mixes in at least one is not saturating on substring luck (ch.04 §3, the
#: issue-#84 weak-oracle class).
STRONG_ORACLE_KINDS: frozenset[str] = frozenset({"predicate", "rubric", "json_schema"})
#: Oracle kinds that saturate: an exact substring / regex either matches or
#: does not, so it stops discriminating the moment a candidate clears it.
WEAK_ORACLE_KINDS: frozenset[str] = frozenset({"expected_text", "regex"})

#: An inline judge criterion shorter than this many words is underspecified —
#: too little text to pin down what "fires" means (ch.04 §10).
MIN_CRITERION_WORDS: int = 8

#: Two-sided confidence for the power analysis' minimum detectable Δscalar.
POWER_CONFIDENCE: float = 0.95
#: The margin is only ``attend`` (not ``sound``) when the min detectable Δ sits
#: within this factor of it — a margin that barely clears the MDD is fragile.
POWER_ATTEND_FACTOR: float = 2.0
#: Never recommend more than this many replicates — beyond it the cost is a
#: contract redesign, not a knob bump (ch.04 §3: power is bought with
#: replication, but the honest budget is finite).
MAX_REPLICATE_BUMP: int = 8

#: A single namespace / judge taking more than this share of the summed
#: absolute loss contribution is a monoculture — it optimizes one blind spot
#: (ch.04 §1.5, the namespace-aggregate decomposition).
MONOCULTURE_SHARE: float = 0.85

#: How far above the board's median wall-clock budget an entry sits before it
#: dominates the round — the builder's own ``entry_budget_outlier`` factor.
BUDGET_OUTLIER_FACTOR: float = 10.0

#: A noise floor measured this many lineage rounds ago (or more) on an active
#: epoch is stale — the gate is calibrated against yesterday's noise (ch.04 §4).
STALE_CALIBRATION_ROUNDS: int = 10

#: With at least this many promotions on record and no placebo cadence set, the
#: gate's discrimination has never been controlled-for (ch.04 §11).
PLACEBO_PROMOTIONS_THRESHOLD: int = 10

#: Down-weight a divergent-but-default-weighted judge is nudged toward — an
#: advisory starting point, not a fitted value (mirrors findings.FP_DOWNWEIGHT).
ADVISORY_DOWNWEIGHT: float = 0.5
#: Spread of measured judge disagreement rates above which leaving a worse-end
#: judge at the default weight is worth attending to (ch.04 §10).
WEIGHT_DIVERGENCE_DELTA: float = 0.2

#: Multiplier applied to the noise floor when recommending a margin clear of it
#: (BOARD-REFLECTION.md §"margin from noise floor": 2–3× the noise SD).
MARGIN_FLOOR_MULTIPLE: float = 2.5

# Check ids.
CHECK_ORACLE_MIX = "oracle_mix"
CHECK_JUDGE_CRITERION_QUALITY = "judge_criterion_quality"
CHECK_STATISTICAL_POWER = "statistical_power"
CHECK_OVERFITTING_POSTURE = "overfitting_posture"
CHECK_LOSS_MONOCULTURE = "loss_monoculture"
CHECK_BUDGET_SANITY = "budget_sanity"
CHECK_CALIBRATION_FRESHNESS = "calibration_freshness"
CHECK_PLACEBO_OUTCOMES = "placebo_outcomes"
CHECK_GENERALIZATION_TREND = "generalization_trend"
CHECK_PROMOTION_HYGIENE = "promotion_hygiene"
CHECK_WEIGHT_REVISIT = "weight_revisit"


# ---------------------------------------------------------------------------
# Result shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PracticeCheck:
    """One practice check's verdict — an affirmation or a diagnosis.

    ``headline`` is a single sentence with the numbers inline; ``rationale`` is
    the one-line doctrine grounding; ``proposed_op`` (present only for the
    mechanically-fixable checks) is a signature-validated ``{op, args}`` builder
    payload; ``unmeasured_reason`` names the missing input for an ``unmeasured``
    verdict (``None`` otherwise).
    """

    check_id: str
    verdict: str
    headline: str
    evidence: dict[str, Any]
    rationale: str
    proposed_op: dict[str, Any] | None = None
    unmeasured_reason: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "verdict": self.verdict,
            "headline": self.headline,
            "evidence": dict(self.evidence),
            "rationale": self.rationale,
            "proposed_op": dict(self.proposed_op) if self.proposed_op is not None else None,
            "unmeasured_reason": self.unmeasured_reason,
        }


@dataclass(frozen=True, slots=True)
class PracticeReview:
    """The practice review over one contract — a list of :class:`PracticeCheck`."""

    checks: tuple[PracticeCheck, ...] = ()

    def verdict_counts(self) -> dict[str, int]:
        counts = {
            VERDICT_SOUND: 0,
            VERDICT_ATTEND: 0,
            VERDICT_UNSOUND: 0,
            VERDICT_UNMEASURED: 0,
        }
        for check in self.checks:
            counts[check.verdict] = counts.get(check.verdict, 0) + 1
        return counts

    def by_verdict(self, verdict: str) -> list[PracticeCheck]:
        return [c for c in self.checks if c.verdict == verdict]

    def to_json(self) -> dict[str, Any]:
        return {
            "checks": [c.to_json() for c in self.checks],
            "verdict_counts": self.verdict_counts(),
        }


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


def _op(op_name: str, args: dict[str, Any]) -> dict[str, Any] | None:
    """A signature-validated ``{op, args}`` payload, or ``None`` if it would not apply.

    Delegates to :func:`zicato.reflection.findings.validate_proposed_op` (the
    same emit-time signature check the findings use), lazily imported to keep
    this engine's package-init import surface light. A payload the builder would
    reject degrades to ``None`` rather than shipping a broken recommendation.
    """
    from zicato.reflection.findings import validate_proposed_op  # noqa: PLC0415

    try:
        return validate_proposed_op(op_name, args)
    except ValueError:
        return None


def _attr_or_key(obj: Any, name: str) -> Any:
    """Read ``name`` off an object or a mapping (scorecards / experiments come as both)."""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _outcome(experiment: Any) -> Any:
    return _attr_or_key(experiment, "outcome")


def _tournament_decision(experiment: Any) -> str:
    outcome = _outcome(experiment)
    if outcome is None:
        return ""
    return str(_attr_or_key(outcome, "tournament_decision") or "")


def _generation_id(experiment: Any) -> str:
    return str(_attr_or_key(experiment, "generation_id") or "")


def _expectation_kind(entry: Any) -> str | None:
    exp = getattr(entry, "expectation", None)
    if exp is None:
        return None
    kind = getattr(exp, "kind", None)
    return str(getattr(kind, "value", kind)) if kind is not None else None


def _inline_judges(board_entries: list[Any]) -> list[tuple[str, str]]:
    """``[(judge_name, criterion_body)]`` for every INLINE judge on the board."""
    out: list[tuple[str, str]] = []
    for entry in board_entries:
        for judge in getattr(entry, "judges", ()) or ():
            mode = getattr(judge, "mode", None)
            if str(getattr(mode, "value", mode)) == "inline":
                out.append((str(getattr(judge, "name", "")), str(getattr(judge, "body", ""))))
    return out


def _criterion_is_weak(body: str) -> bool:
    """Whether an inline criterion is underspecified — too short or noun-free.

    Two shape-only heuristics (ch.04 §10 names underspecified criteria as the
    root of ambiguous adjudications, but the *measurement* is the ambiguous
    pile — this pre-check only flags the shape): fewer than
    :data:`MIN_CRITERION_WORDS` words, or no "concrete noun" proxy (no token of
    ≥4 letters that is not a common function/verb word). Deliberately
    conservative — a false flag is only an ``attend``, never an ``unsound``,
    unless the measured ambiguous pile confirms it.
    """
    words = [w for w in "".join(c if c.isalnum() else " " for c in body).split() if w]
    if len(words) < MIN_CRITERION_WORDS:
        return True
    _thin = {
        "the",
        "and",
        "that",
        "this",
        "with",
        "when",
        "does",
        "must",
        "should",
        "have",
        "into",
        "from",
        "then",
        "than",
        "they",
        "them",
        "will",
        "what",
        "over",
        "each",
        "some",
        "such",
        "make",
        "made",
        "been",
        "were",
        "your",
    }
    return not any(len(w) >= 4 and w.lower() not in _thin for w in words)


def _rounds_since_generation(experiments: list[Any], generation_id: Any) -> int | None:
    """How many lineage rounds have elapsed since ``generation_id`` (``None`` if unknown)."""
    if not isinstance(generation_id, str) or not generation_id:
        return None
    ids = [_generation_id(e) for e in experiments]
    if generation_id not in ids:
        return None
    return len(ids) - 1 - ids.index(generation_id)


def _train_board_size(board_entries: list[Any], overfitting: Any) -> int:
    """The train-slice size the tournament actually scores (split honored)."""
    try:
        from zicato.board.split import split_board  # noqa: PLC0415

        train_ids, _holdout = split_board(board_entries, overfitting)
        if train_ids:
            return len(train_ids)
    except Exception:  # noqa: BLE001 — a malformed split degrades to the full board
        pass
    return len(board_entries)


# ---------------------------------------------------------------------------
# Contract-shape checks
# ---------------------------------------------------------------------------


def check_oracle_mix(*, board_entries: list[Any]) -> PracticeCheck:
    """All-``expected_text``/``regex`` oracles saturate — the issue-#84 class (ch.04 §3)."""
    kinds = [k for k in (_expectation_kind(e) for e in board_entries) if k]
    n_strong = sum(1 for k in kinds if k in STRONG_ORACLE_KINDS)
    n_weak = sum(1 for k in kinds if k in WEAK_ORACLE_KINDS)
    evidence = {
        "expectation_kinds": sorted(set(kinds)),
        "n_expectations": len(kinds),
        "n_strong": n_strong,
        "n_weak": n_weak,
    }
    rationale = "weak substring/regex oracles saturate — the issue-#84 class (ch.04 §3)."
    if not kinds:
        return PracticeCheck(
            check_id=CHECK_ORACLE_MIX,
            verdict=VERDICT_UNMEASURED,
            headline="No board entry declares an outcome expectation — no oracles to assess.",
            evidence=evidence,
            rationale=rationale,
            unmeasured_reason="no board entry carries an outcome expectation",
        )
    if n_strong == 0:
        return PracticeCheck(
            check_id=CHECK_ORACLE_MIX,
            verdict=VERDICT_UNSOUND,
            headline=(
                f"All {len(kinds)} board oracle(s) are exact-text/regex "
                f"({', '.join(sorted(set(kinds)))}) — they saturate the moment a candidate "
                "clears them and stop discriminating."
            ),
            evidence=evidence,
            rationale=rationale,
            # Authoring decision — naming the board editor, not a mechanical op.
        )
    return PracticeCheck(
        check_id=CHECK_ORACLE_MIX,
        verdict=VERDICT_SOUND,
        headline=(
            f"The board mixes {n_strong} structured oracle(s) "
            f"(predicate/rubric/json_schema) with {n_weak} substring/regex — the oracles "
            "do not all saturate together."
        ),
        evidence=evidence,
        rationale=rationale,
    )


def check_judge_criterion_quality(
    *, board_entries: list[Any], scorecards: list[dict[str, Any]] | None
) -> PracticeCheck:
    """Underspecified inline criteria breed ambiguous adjudications (ch.04 §10)."""
    judges = _inline_judges(board_entries)
    rationale = "underspecified criteria breed ambiguous adjudications (ch.04 §10)."
    if not judges:
        return PracticeCheck(
            check_id=CHECK_JUDGE_CRITERION_QUALITY,
            verdict=VERDICT_UNMEASURED,
            headline="No inline judge criteria on the board to assess.",
            evidence={"n_inline_judges": 0},
            rationale=rationale,
            unmeasured_reason="the board declares no inline judges",
        )
    flagged = [name for name, body in judges if _criterion_is_weak(body)]
    evidence: dict[str, Any] = {
        "n_inline_judges": len(judges),
        "flagged_judges": flagged,
        "min_criterion_words": MIN_CRITERION_WORDS,
    }
    if not flagged:
        return PracticeCheck(
            check_id=CHECK_JUDGE_CRITERION_QUALITY,
            verdict=VERDICT_SOUND,
            headline=(
                f"All {len(judges)} inline judge criteria are specific "
                f"(≥{MIN_CRITERION_WORDS} words, concrete) — well-posed for adjudication."
            ),
            evidence=evidence,
            rationale=rationale,
        )
    # When scorecards exist, a flagged judge with a MEASURED ambiguous pile
    # upgrades attend -> unsound: the shape suspicion is now confirmed.
    by_name = {str(c.get("judge_name")): c for c in (scorecards or [])}
    confirmed = [name for name in flagged if bool(by_name.get(name, {}).get("ambiguous_pile"))]
    if confirmed:
        piles = {n: by_name[n].get("ambiguous") for n in confirmed}
        evidence["ambiguous_piles"] = piles
        return PracticeCheck(
            check_id=CHECK_JUDGE_CRITERION_QUALITY,
            verdict=VERDICT_UNSOUND,
            headline=(
                f"Underspecified judge(s) {', '.join(confirmed)} carry a measured ambiguous "
                f"pile ({', '.join(f'{n}: {piles[n]}' for n in confirmed)}) — the vague "
                "criterion is confirmed to defeat adjudication."
            ),
            evidence=evidence,
            rationale=rationale,
        )
    return PracticeCheck(
        check_id=CHECK_JUDGE_CRITERION_QUALITY,
        verdict=VERDICT_ATTEND,
        headline=(
            f"Inline judge(s) {', '.join(flagged)} have a thin criterion "
            f"(<{MIN_CRITERION_WORDS} words or no concrete noun) — likely to adjudicate "
            "ambiguously."
        ),
        evidence=evidence,
        rationale=rationale,
    )


def check_statistical_power(
    *,
    weights: Any,
    board_entries: list[Any],
    noise_floor: dict[str, Any] | None,
) -> PracticeCheck:
    """A loop whose min detectable Δ exceeds the margin is theater at this power (ch.04 §3).

    ``k`` comes from the CONTRACT (``params["replicates"]``, falling back to
    the structure's default), and every path now honours it — including the
    gauntlet under ``--mode fast``, which used to ignore it outright (issue
    #109). One caveat the contract cannot express: fast mode replicates the
    CHALLENGER against a frozen cached champion aggregate, so the contrast
    keeps one unreplicated side and ``power_analysis``'s two-sample
    ``sqrt(2/(k·n))`` is optimistic there by roughly ``sqrt((k+1)/2)``. The
    runtime mode is not a contract field, so this check cannot gate on it;
    ``--mode full`` is the configuration the formula actually describes.
    """
    from zicato.reflection.analysis import power_analysis, sigma_from_noise_floor  # noqa: PLC0415
    from zicato.selection.registry import default_replicates_for  # noqa: PLC0415

    rationale = "when the min detectable Δ exceeds the margin the loop is theater (ch.04 §3, §13)."
    sigma = sigma_from_noise_floor(noise_floor)
    if sigma is None:
        return PracticeCheck(
            check_id=CHECK_STATISTICAL_POWER,
            verdict=VERDICT_UNMEASURED,
            headline="No A/A noise floor with raw draws — the loop's power cannot be computed.",
            evidence={"needs": "noise_floor.scalars"},
            rationale=rationale,
            unmeasured_reason="no measured A/A noise floor — run `zicato board audit`",
        )
    ts = getattr(weights, "tournament_structure", None)
    params = getattr(ts, "params", {}) or {}
    structure = str(getattr(ts, "structure", ""))
    try:
        k = max(1, int(params.get("replicates", default_replicates_for(structure))))
    except (TypeError, ValueError):
        k = max(1, default_replicates_for(structure))
    n = _train_board_size(board_entries, getattr(weights, "overfitting", None))
    margin = float(getattr(weights, "promote_margin", 0.0))
    pa = power_analysis(sigma=sigma, k=k, n=n, confidence=POWER_CONFIDENCE)
    mdd = float(pa["min_detectable_delta"])
    evidence = {
        "sigma": sigma,
        "replicates": k,
        "train_board_size": n,
        "promote_margin": margin,
        "min_detectable_delta": mdd,
    }

    # k needed to bring MDD under the margin: MDD ∝ 1/sqrt(k), so
    # k_needed = ceil(2 · z² · σ² / (n · margin²)); capped at MAX_REPLICATE_BUMP.
    def _replicate_op() -> dict[str, Any] | None:
        if margin <= 0 or n <= 0:
            return None
        z = float(pa["z"])
        k_needed = math.ceil((2.0 * z * z * sigma * sigma) / (n * margin * margin))
        k_target = min(max(k_needed, k + 1), MAX_REPLICATE_BUMP)
        if k_target <= k:
            return None
        evidence["recommended_replicates"] = k_target
        return _op("set_param", {"key": "replicates", "value": k_target})

    if mdd > margin:
        return PracticeCheck(
            check_id=CHECK_STATISTICAL_POWER,
            verdict=VERDICT_UNSOUND,
            headline=(
                f"The min detectable Δscalar ({mdd:.4g}) exceeds promote_margin ({margin:.4g}) "
                f"at {k} replicate(s) × {n} train entries — the loop cannot see a real "
                "improvement this small; it is theater at this power."
            ),
            evidence=evidence,
            rationale=rationale,
            proposed_op=_replicate_op(),
        )
    if mdd * POWER_ATTEND_FACTOR > margin:
        return PracticeCheck(
            check_id=CHECK_STATISTICAL_POWER,
            verdict=VERDICT_ATTEND,
            headline=(
                f"The min detectable Δscalar ({mdd:.4g}) is within {POWER_ATTEND_FACTOR:g}× of "
                f"promote_margin ({margin:.4g}) — the margin barely clears the noise; power is "
                "thin."
            ),
            evidence=evidence,
            rationale=rationale,
            proposed_op=_replicate_op(),
        )
    ratio = margin / mdd if mdd > 0 else math.inf
    return PracticeCheck(
        check_id=CHECK_STATISTICAL_POWER,
        verdict=VERDICT_SOUND,
        headline=(
            f"promote_margin ({margin:.4g}) clears the min detectable Δscalar ({mdd:.4g}) by "
            f"{ratio:.1f}× at {k} replicate(s) × {n} train entries — the loop has power to "
            "spare."
        ),
        evidence=evidence,
        rationale=rationale,
    )


def check_overfitting_posture(
    *, weights: Any, board_entries: list[Any], experiments: list[Any]
) -> PracticeCheck:
    """Memorization defense must scale with a splittable board (OVERFITTING.md §4/§6/§7)."""
    of = getattr(weights, "overfitting", None)
    pq = getattr(weights, "proposer_quality", None)
    rationale = "memorization defense must scale with a splittable board (OVERFITTING.md §4/§6/§7)."
    board_size = len(board_entries)
    min_split = int(getattr(of, "min_board_size_for_split", 6)) if of is not None else 6
    promotions = sum(1 for e in experiments if _tournament_decision(e) == "promoted")
    evidence = {
        "board_size": board_size,
        "min_board_size_for_split": min_split,
        "holdout_enabled": bool(getattr(of, "enabled", False)),
        "rotate_holdout": bool(getattr(of, "rotate_holdout", False)),
        "random_baseline_every_n": int(getattr(of, "random_baseline_every_n", 0)),
        "screen_entries": int(getattr(pq, "screen_entries", 0)) if pq is not None else 0,
        "promotions": promotions,
    }

    if board_size < min_split:
        return PracticeCheck(
            check_id=CHECK_OVERFITTING_POSTURE,
            verdict=VERDICT_SOUND,
            headline=(
                f"The board of {board_size} entries is below the {min_split}-entry split floor — "
                "it legitimately cannot hold out a slice, so the overfitting machine is a no-op "
                "here (sound, with that caveat)."
            ),
            evidence=evidence,
            rationale=rationale,
        )

    issues: list[str] = []
    op: dict[str, Any] | None = None
    if not getattr(of, "enabled", False):
        issues.append("holdout split disabled on a splittable board")
        op = op or _op("set_holdout", {"enabled": True})
    elif not getattr(of, "rotate_holdout", False):
        issues.append("holdout is not rotating (a fixed slice is mined every epoch)")
        op = op or _op("set_holdout", {"rotate_holdout": True})
    if (
        getattr(of, "random_baseline_every_n", 0) == 0
        and promotions >= PLACEBO_PROMOTIONS_THRESHOLD
    ):
        issues.append(f"no placebo cadence after {promotions} promotions")
        op = op or _op("set_holdout", {"random_baseline_every_n": PLACEBO_PROMOTIONS_THRESHOLD})
    if pq is not None and getattr(pq, "screen_entries", 0) == 0:
        issues.append("pre-tournament candidate screening is off")
        op = op or _op("set_screening", {"entries": 2})

    if issues:
        return PracticeCheck(
            check_id=CHECK_OVERFITTING_POSTURE,
            verdict=VERDICT_ATTEND,
            headline=(f"On a splittable board of {board_size} entries: " + "; ".join(issues) + "."),
            evidence=evidence,
            rationale=rationale,
            proposed_op=op,
        )
    return PracticeCheck(
        check_id=CHECK_OVERFITTING_POSTURE,
        verdict=VERDICT_SOUND,
        headline=(
            f"On a splittable board of {board_size} entries the holdout splits and rotates, the "
            "placebo cadence is covered, and screening is on — the overfitting posture is sound."
        ),
        evidence=evidence,
        rationale=rationale,
    )


def check_loss_monoculture(*, weights: Any, corpus_stats: dict[str, Any] | None) -> PracticeCheck:
    """A monoculture loss optimizes one blind spot (ch.04 §1.5)."""
    rationale = "a monoculture loss optimizes one blind spot (ch.04 §1.5)."
    contributions = (corpus_stats or {}).get("term_contributions")
    if not isinstance(contributions, dict) or not contributions:
        return PracticeCheck(
            check_id=CHECK_LOSS_MONOCULTURE,
            verdict=VERDICT_UNMEASURED,
            headline="No corpus loss decomposition — per-term contribution shares are unmeasured.",
            evidence={"needs": "corpus_stats.term_contributions"},
            rationale=rationale,
            unmeasured_reason=(
                "no measured per-term loss contributions — run `zicato reflect run`"
            ),
        )
    abs_by_term = {str(t): abs(float(v)) for t, v in contributions.items()}
    total = sum(abs_by_term.values())
    if total <= 0:
        return PracticeCheck(
            check_id=CHECK_LOSS_MONOCULTURE,
            verdict=VERDICT_UNMEASURED,
            headline="Every loss term contributed zero over the corpus — nothing moved the scalar.",
            evidence={"term_contributions": abs_by_term},
            rationale=rationale,
            unmeasured_reason="all loss terms were flat over the corpus (no signal to apportion)",
        )
    top_term, top_abs = max(abs_by_term.items(), key=lambda kv: kv[1])
    top_share = top_abs / total
    evidence = {
        "term_contributions": abs_by_term,
        "dominant_term": top_term,
        "dominant_share": top_share,
        "monoculture_share": MONOCULTURE_SHARE,
    }
    if top_share > MONOCULTURE_SHARE:
        # Advisory sketch: when the dominant term is a scored NAMESPACE, halve
        # its coefficient so the rest of the loss can register.
        op: dict[str, Any] | None = None
        namespace = top_term.split(":", 1)[0] + ":" if ":" in top_term else ""
        ns_weights = dict(getattr(weights, "namespace_weights", {}) or {})
        if namespace and namespace in ns_weights:
            sketch = dict(ns_weights)
            sketch[namespace] = round(float(sketch[namespace]) / 2.0, 6)
            op = _op("set_namespace_weights", {"namespace_weights": sketch})
        return PracticeCheck(
            check_id=CHECK_LOSS_MONOCULTURE,
            verdict=VERDICT_ATTEND,
            headline=(
                f"One term ({top_term}) carries {top_share:.0%} of the scalar's loss — the loss "
                "is a monoculture optimizing a single blind spot."
            ),
            evidence=evidence,
            rationale=rationale,
            proposed_op=op,
        )
    return PracticeCheck(
        check_id=CHECK_LOSS_MONOCULTURE,
        verdict=VERDICT_SOUND,
        headline=(
            f"No single loss term exceeds {MONOCULTURE_SHARE:.0%} of the scalar "
            f"(top: {top_term} at {top_share:.0%}) — the loss is balanced."
        ),
        evidence=evidence,
        rationale=rationale,
    )


def check_budget_sanity(*, board_entries: list[Any]) -> PracticeCheck:
    """A >10×-median entry dominates the round's wall-clock (builder validate heuristic)."""
    rationale = "a >10×-median-budget entry dominates the round's wall-clock (builder heuristic)."
    budgets = [int(getattr(e, "wall_clock_budget_seconds", 0)) for e in board_entries]
    if len(budgets) < 2:
        return PracticeCheck(
            check_id=CHECK_BUDGET_SANITY,
            verdict=VERDICT_UNMEASURED,
            headline="Fewer than two board entries — no median wall-clock budget to compare.",
            evidence={"n_entries": len(budgets)},
            rationale=rationale,
            unmeasured_reason="fewer than two board entries carry a wall-clock budget",
        )
    median = statistics.median(budgets)
    outliers = [
        str(getattr(e, "id", ""))
        for e in board_entries
        if median > 0
        and int(getattr(e, "wall_clock_budget_seconds", 0)) > BUDGET_OUTLIER_FACTOR * median
    ]
    evidence = {
        "median_budget_s": median,
        "outlier_entries": outliers,
        "factor": BUDGET_OUTLIER_FACTOR,
    }
    if outliers:
        return PracticeCheck(
            check_id=CHECK_BUDGET_SANITY,
            verdict=VERDICT_ATTEND,
            headline=(
                f"Entry(ies) {', '.join(outliers)} exceed {BUDGET_OUTLIER_FACTOR:g}× the board "
                f"median budget ({median:g}s) — they dominate the round's wall-clock."
            ),
            evidence=evidence,
            rationale=rationale,
        )
    return PracticeCheck(
        check_id=CHECK_BUDGET_SANITY,
        verdict=VERDICT_SOUND,
        headline=(
            f"No entry exceeds {BUDGET_OUTLIER_FACTOR:g}× the board median budget ({median:g}s) — "
            "wall-clock budgets are balanced."
        ),
        evidence=evidence,
        rationale=rationale,
    )


# ---------------------------------------------------------------------------
# Operating-history checks
# ---------------------------------------------------------------------------


def check_calibration_freshness(
    *,
    weights: Any,
    noise_floor: dict[str, Any] | None,
    experiments: list[Any],
    epoch_cfg: Any,
) -> PracticeCheck:
    """A stale floor calibrates today's gate against yesterday's noise (ch.04 §4)."""
    rationale = "a stale noise floor calibrates today's gate against yesterday's noise (ch.04 §4)."
    if not isinstance(noise_floor, dict):
        return PracticeCheck(
            check_id=CHECK_CALIBRATION_FRESHNESS,
            verdict=VERDICT_UNMEASURED,
            headline="No A/A noise floor on the epoch record — the gate has never been calibrated.",
            evidence={"needs": "epoch noise_floor"},
            rationale=rationale,
            unmeasured_reason="no noise floor measured — run `zicato board audit`",
        )
    try:
        floor_max = float(noise_floor.get("max_abs_delta", 0.0))
    except (TypeError, ValueError):
        floor_max = 0.0
    margin = float(getattr(weights, "promote_margin", 0.0))
    ratio = margin / floor_max if floor_max > 0 else None
    rounds_since = _rounds_since_generation(experiments, noise_floor.get("generation_id"))
    active = epoch_cfg is None or not bool(getattr(epoch_cfg, "closed", False))
    evidence = {
        "noise_floor_max_abs_delta": floor_max,
        "promote_margin": margin,
        "margin_over_floor": ratio,
        "rounds_since_measured": rounds_since,
        "active_epoch": active,
        "stale_after_rounds": STALE_CALIBRATION_ROUNDS,
    }
    if rounds_since is not None and rounds_since >= STALE_CALIBRATION_ROUNDS and active:
        return PracticeCheck(
            check_id=CHECK_CALIBRATION_FRESHNESS,
            verdict=VERDICT_ATTEND,
            headline=(
                f"The noise floor was measured {rounds_since} rounds ago on an active epoch "
                f"(≥{STALE_CALIBRATION_ROUNDS}) — re-audit before trusting the margin."
            ),
            evidence=evidence,
            rationale=rationale,
            # Re-measuring is a CLI action (`zicato board audit`), not a builder op.
        )
    age = f"{rounds_since} round(s) ago" if rounds_since is not None else "recently"
    if ratio is not None:
        tail = f"; promote_margin clears it {ratio:.1f}×"
    else:
        tail = (
            "; the floor is 0.0 (a quiet/deterministic harness) so the margin trivially clears it"
        )
    return PracticeCheck(
        check_id=CHECK_CALIBRATION_FRESHNESS,
        verdict=VERDICT_SOUND,
        headline=f"The noise floor was measured {age}{tail} — the calibration is fresh.",
        evidence=evidence,
        rationale=rationale,
    )


def check_placebo_outcomes(*, weights: Any, experiments: list[Any]) -> PracticeCheck:
    """A rejected placebo proves gate discrimination; a promoted one disproves it (ch.04 §11)."""
    from zicato.health.diagnostics import (  # noqa: PLC0415
        _is_placebo_experiment,
        detect_placebo_promoted,
    )

    rationale = "a rejected placebo proves gate discrimination; a promoted one disproves it (§11)."
    of = getattr(weights, "overfitting", None)
    cadence = int(getattr(of, "random_baseline_every_n", 0)) if of is not None else 0
    promoted = detect_placebo_promoted(experiments)
    placebo_exps = [e for e in experiments if _is_placebo_experiment(e)]
    evidence: dict[str, Any] = {
        "random_baseline_every_n": cadence,
        "n_placebo_experiments": len(placebo_exps),
        "n_promoted_placebos": len(promoted),
    }
    if promoted:
        evidence["promoted_generation_ids"] = [f.detail.get("generation_id") for f in promoted]
        return PracticeCheck(
            check_id=CHECK_PLACEBO_OUTCOMES,
            verdict=VERDICT_UNSOUND,
            headline=(
                f"A random-baseline placebo was PROMOTED ({len(promoted)} time(s)) — a "
                "semantics-preserving no-op won a tournament, so gate discrimination is broken "
                "and recent wins are suspect."
            ),
            evidence=evidence,
            rationale=rationale,
        )
    if placebo_exps:
        return PracticeCheck(
            check_id=CHECK_PLACEBO_OUTCOMES,
            verdict=VERDICT_SOUND,
            headline=(
                f"The gate rejected every placebo challenger it saw ({len(placebo_exps)}) — "
                "discrimination holds."
            ),
            evidence=evidence,
            rationale=rationale,
        )
    if cadence > 0:
        return PracticeCheck(
            check_id=CHECK_PLACEBO_OUTCOMES,
            verdict=VERDICT_ATTEND,
            headline=(
                f"A placebo cadence is set (every {cadence} rounds) but no placebo challenger has "
                "fired yet — run more rounds (or lower the cadence) to exercise the control arm."
            ),
            evidence=evidence,
            rationale=rationale,
        )
    return PracticeCheck(
        check_id=CHECK_PLACEBO_OUTCOMES,
        verdict=VERDICT_UNMEASURED,
        headline="No placebo arm configured (random_baseline_every_n=0) — discrimination unproven.",
        evidence=evidence,
        rationale=rationale,
        proposed_op=_op("set_holdout", {"random_baseline_every_n": PLACEBO_PROMOTIONS_THRESHOLD}),
        unmeasured_reason="no placebo control arm has run (set random_baseline_every_n)",
    )


def check_generalization_trend(*, weights: Any, experiments: list[Any]) -> PracticeCheck:
    """A widening holdout gap is board memorization (OVERFITTING.md §6/§7)."""
    from zicato.health.diagnostics import (  # noqa: PLC0415
        _gap_observation,
        detect_generalization_gap,
    )

    rationale = "a widening holdout/train gap is board memorization (OVERFITTING.md §6/§7)."
    of = getattr(weights, "overfitting", None)
    rotate = bool(getattr(of, "rotate_holdout", False)) if of is not None else False
    n_holdout = sum(1 for e in experiments if _gap_observation(e) is not None)
    findings = detect_generalization_gap(experiments)
    evidence: dict[str, Any] = {"generations_with_holdout": n_holdout, "rotate_holdout": rotate}
    if findings:
        finding = max(findings, key=lambda f: 0 if f.severity == "critical" else 1)
        evidence["generalization_gap"] = finding.detail.get("generalization_gap")
        evidence["severity"] = finding.severity
        op = None if rotate else _op("set_holdout", {"rotate_holdout": True})
        return PracticeCheck(
            check_id=CHECK_GENERALIZATION_TREND,
            verdict=VERDICT_UNSOUND,
            headline=(
                f"The holdout/train gap widened to "
                f"{finding.detail.get('generalization_gap'):+.3f} over {n_holdout} generations — "
                "the champion is memorizing the board."
            ),
            evidence=evidence,
            rationale=rationale,
            proposed_op=op,
        )
    if n_holdout >= 2:
        note = "with the holdout rotating" if rotate else "though the holdout is NOT rotating"
        return PracticeCheck(
            check_id=CHECK_GENERALIZATION_TREND,
            verdict=VERDICT_SOUND,
            headline=(
                f"The holdout/train gap is flat or shrinking over {n_holdout} generations "
                f"({note}) — your board is not being memorized."
            ),
            evidence=evidence,
            rationale=rationale,
        )
    return PracticeCheck(
        check_id=CHECK_GENERALIZATION_TREND,
        verdict=VERDICT_UNMEASURED,
        headline="Fewer than two generations carry a measured holdout — the trend is unmeasured.",
        evidence=evidence,
        rationale=rationale,
        unmeasured_reason="fewer than two generations have a holdout-scored outcome",
    )


def check_promotion_hygiene(
    *,
    weights: Any,
    experiments: list[Any],
    board_entries: list[Any],
    noise_floor: dict[str, Any] | None,
) -> PracticeCheck:
    """A promotion on a sub-floor margin with no evidence gate promotes noise (ch.04 §3/§6)."""
    from zicato.health.diagnostics import detect_margin_below_noise_floor  # noqa: PLC0415
    from zicato.selection.evidence_gate import read_promote_confidence_threshold  # noqa: PLC0415
    from zicato.tournament.calibration import margin_below_floor  # noqa: PLC0415

    rationale = "a promotion on a sub-floor margin with no evidence gate promotes noise (§3/§6)."
    promotions = [e for e in experiments if _tournament_decision(e) == "promoted"]
    n = len(promotions)
    if n == 0:
        return PracticeCheck(
            check_id=CHECK_PROMOTION_HYGIENE,
            verdict=VERDICT_UNMEASURED,
            headline=(
                "No promotions under this contract yet — promotion hygiene has nothing to audit."
            ),
            evidence={"promotions": 0},
            rationale=rationale,
            unmeasured_reason="no promotion has been recorded under this contract",
        )
    ts = getattr(weights, "tournament_structure", None)
    params = getattr(ts, "params", {}) or {}
    gate_on = read_promote_confidence_threshold(params) is not None
    margin = float(getattr(weights, "promote_margin", 0.0))
    of = getattr(weights, "overfitting", None)
    board_size = len(board_entries)
    min_split = int(getattr(of, "min_board_size_for_split", 6)) if of is not None else 6
    holdout_on = bool(getattr(of, "enabled", False)) and board_size >= min_split
    below_floor = margin_below_floor(margin, noise_floor)
    # Compose the existing detector (translate its finding into the practice).
    mbf = detect_margin_below_noise_floor(noise_floor, margin, gate_on)
    evidence = {
        "promotions": n,
        "evidence_gate_on": gate_on,
        "promote_margin": margin,
        "holdout_confirms": holdout_on,
        "margin_below_floor": below_floor,
        "margin_below_floor_finding": bool(mbf),
    }
    if below_floor and not gate_on:
        floor_max = (
            float(noise_floor.get("max_abs_delta", 0.0)) if isinstance(noise_floor, dict) else 0.0
        )
        recommended = round(MARGIN_FLOOR_MULTIPLE * floor_max, 6)
        return PracticeCheck(
            check_id=CHECK_PROMOTION_HYGIENE,
            verdict=VERDICT_UNSOUND,
            headline=(
                f"Your {n} promotion(s) were decided by a margin ({margin:.4g}) below the measured "
                f"floor ({floor_max:.4g}) with no evidence gate — they promote on noise."
            ),
            evidence=evidence,
            rationale=rationale,
            proposed_op=_op("set_gate", {"promote_margin": recommended}),
        )
    if gate_on or holdout_on:
        via = "the evidence gate" if gate_on else "holdout confirmation"
        return PracticeCheck(
            check_id=CHECK_PROMOTION_HYGIENE,
            verdict=VERDICT_SOUND,
            headline=(
                f"Your {n} promotion(s) were confirmed via {via} — the gate held them to real "
                "separation, not margin luck."
            ),
            evidence=evidence,
            rationale=rationale,
        )
    if not isinstance(noise_floor, dict):
        return PracticeCheck(
            check_id=CHECK_PROMOTION_HYGIENE,
            verdict=VERDICT_UNMEASURED,
            headline=(
                f"Your {n} promotion(s) rest on the margin alone and the noise floor is "
                "unmeasured — real separation cannot be confirmed."
            ),
            evidence=evidence,
            rationale=rationale,
            unmeasured_reason="no noise floor to check the promotion margin against",
        )
    return PracticeCheck(
        check_id=CHECK_PROMOTION_HYGIENE,
        verdict=VERDICT_ATTEND,
        headline=(
            f"Your {n} promotion(s) rest on the margin alone — it clears the floor, but no "
            "evidence gate or holdout confirms them."
        ),
        evidence=evidence,
        rationale=rationale,
    )


def check_weight_revisit(
    *, weights: Any, board_entries: list[Any], scorecards: list[dict[str, Any]] | None
) -> PracticeCheck:
    """A default-weight judge with divergent measured reliability mis-weights the loss (§10)."""
    rationale = (
        "a default-weight judge with divergent measured reliability mis-weights the loss (§10)."
    )
    if not scorecards:
        return PracticeCheck(
            check_id=CHECK_WEIGHT_REVISIT,
            verdict=VERDICT_UNMEASURED,
            headline="No judge scorecards — measured reliabilities are unavailable.",
            evidence={"needs": "scorecards"},
            rationale=rationale,
            unmeasured_reason=(
                "no judge scorecards — run `zicato reflect run --adjudicator-call-llm`"
            ),
        )
    per_judge = dict(getattr(weights, "per_judge_weights", {}) or {})
    rates = [
        float(c["disagreement_rate"])
        for c in scorecards
        if isinstance(c.get("disagreement_rate"), int | float)
    ]
    default_weighted = [c for c in scorecards if str(c.get("judge_name")) not in per_judge]
    evidence: dict[str, Any] = {
        "n_scorecards": len(scorecards),
        "default_weighted_judges": [str(c.get("judge_name")) for c in default_weighted],
        "disagreement_spread": (max(rates) - min(rates)) if len(rates) >= 2 else None,
    }
    if len(rates) >= 2 and (max(rates) - min(rates)) > WEIGHT_DIVERGENCE_DELTA:
        mean_rate = statistics.fmean(rates)
        worse = [
            c
            for c in default_weighted
            if isinstance(c.get("disagreement_rate"), int | float)
            and float(c["disagreement_rate"]) > mean_rate
        ]
        if worse:
            target = max(worse, key=lambda c: float(c["disagreement_rate"]))
            target_name = str(target.get("judge_name"))
            evidence["down_weight_candidate"] = target_name
            sketch = {
                **{str(k): float(v) for k, v in per_judge.items()},
                target_name: ADVISORY_DOWNWEIGHT,
            }
            return PracticeCheck(
                check_id=CHECK_WEIGHT_REVISIT,
                verdict=VERDICT_ATTEND,
                headline=(
                    f"Judge {target_name} sits at the default weight but its measured "
                    f"disagreement ({float(target['disagreement_rate']):.0%}) is above the corpus "
                    "mean — revisit its weight."
                ),
                evidence=evidence,
                rationale=rationale,
                proposed_op=_op("set_weights", {"per_judge_weights": sketch}),
            )
    return PracticeCheck(
        check_id=CHECK_WEIGHT_REVISIT,
        verdict=VERDICT_SOUND,
        headline=(
            "Measured judge reliabilities are close, or every judge already carries an explicit "
            "weight — the loss is not silently mis-weighted."
        ),
        evidence=evidence,
        rationale=rationale,
    )


# ---------------------------------------------------------------------------
# Corpus summary + the top-level review
# ---------------------------------------------------------------------------


def summarize_corpus(corpus: list[Any]) -> dict[str, Any]:
    """Aggregate the corpus into the ``corpus_stats`` the review consumes.

    Sums the absolute per-term loss contribution across every observation (from
    each :class:`~zicato.reflection.corpus.ObservationRun`'s
    ``loss_decomposition``) — the input :func:`check_loss_monoculture` reads.
    Pure; no I/O.
    """
    term_contributions: dict[str, float] = {}
    for obs in corpus:
        decomp = getattr(obs, "loss_decomposition", None) or {}
        for term, value in decomp.items():
            try:
                term_contributions[str(term)] = term_contributions.get(str(term), 0.0) + abs(
                    float(value)
                )
            except (TypeError, ValueError):
                continue
    return {"term_contributions": term_contributions, "n_observations": len(corpus)}


def review_practices(
    *,
    board_entries: list[Any],
    board_meta: dict[str, Any] | None,
    weights: Any,
    epoch_cfg: Any,
    experiments: list[Any],
    scorecards: list[dict[str, Any]] | None = None,
    corpus_stats: dict[str, Any] | None = None,
    noise_floor: dict[str, Any] | None = None,
    preflight: dict[str, Any] | None = None,
) -> PracticeReview:
    """Run every practice check over the contract + history + reflection artifacts.

    PURE and zero-LLM. ``noise_floor`` / ``preflight`` fall back to the epoch
    record's own fields when not passed explicitly. Checks whose inputs are
    absent (``scorecards`` / ``corpus_stats`` on the cheap tier) return an
    honest ``unmeasured`` verdict naming the missing input, never a fabricated
    one. The result carries every verdict — the ``sound`` affirmations included.
    """
    _ = board_meta  # accepted for signature completeness (judge_only / disable_drift context)
    if noise_floor is None and epoch_cfg is not None:
        raw = getattr(epoch_cfg, "noise_floor", None)
        noise_floor = raw if isinstance(raw, dict) else None
    if preflight is None and epoch_cfg is not None:
        raw = getattr(epoch_cfg, "preflight", None)
        preflight = raw if isinstance(raw, dict) else None
    _ = preflight  # threaded for future checks; no current check consumes it

    checks = (
        check_oracle_mix(board_entries=board_entries),
        check_judge_criterion_quality(board_entries=board_entries, scorecards=scorecards),
        check_statistical_power(
            weights=weights, board_entries=board_entries, noise_floor=noise_floor
        ),
        check_overfitting_posture(
            weights=weights, board_entries=board_entries, experiments=experiments
        ),
        check_loss_monoculture(weights=weights, corpus_stats=corpus_stats),
        check_budget_sanity(board_entries=board_entries),
        check_calibration_freshness(
            weights=weights, noise_floor=noise_floor, experiments=experiments, epoch_cfg=epoch_cfg
        ),
        check_placebo_outcomes(weights=weights, experiments=experiments),
        check_generalization_trend(weights=weights, experiments=experiments),
        check_promotion_hygiene(
            weights=weights,
            experiments=experiments,
            board_entries=board_entries,
            noise_floor=noise_floor,
        ),
        check_weight_revisit(weights=weights, board_entries=board_entries, scorecards=scorecards),
    )
    return PracticeReview(checks=checks)


def rank_checks_for_report(review: PracticeReview) -> list[PracticeCheck]:
    """Report ordering: ``sound`` affirmations first (they teach), then the
    deficiencies worst-first (``unsound`` above ``attend``), then ``unmeasured``.

    The affirmation-first stance is the doc's editorial call: reflection's intent
    is that sound practice teaches as much as a deficiency flag, so the operator
    reads what they are doing right before the deficiencies — which then land
    against that baseline. Within each band, catalog order is preserved (stable).
    """
    catalog_order = {c.check_id: i for i, c in enumerate(review.checks)}
    band = {VERDICT_SOUND: 0, VERDICT_UNSOUND: 1, VERDICT_ATTEND: 2, VERDICT_UNMEASURED: 3}
    return sorted(
        review.checks,
        key=lambda c: (band.get(c.verdict, 9), catalog_order.get(c.check_id, 99)),
    )


__all__ = [
    "ADVISORY_DOWNWEIGHT",
    "BUDGET_OUTLIER_FACTOR",
    "MARGIN_FLOOR_MULTIPLE",
    "MAX_REPLICATE_BUMP",
    "MIN_CRITERION_WORDS",
    "MONOCULTURE_SHARE",
    "PLACEBO_PROMOTIONS_THRESHOLD",
    "POWER_ATTEND_FACTOR",
    "STALE_CALIBRATION_ROUNDS",
    "STRONG_ORACLE_KINDS",
    "VERDICT_ATTEND",
    "VERDICT_SOUND",
    "VERDICT_UNMEASURED",
    "VERDICT_UNSOUND",
    "WEAK_ORACLE_KINDS",
    "WEIGHT_DIVERGENCE_DELTA",
    "PracticeCheck",
    "PracticeReview",
    "check_budget_sanity",
    "check_calibration_freshness",
    "check_generalization_trend",
    "check_judge_criterion_quality",
    "check_loss_monoculture",
    "check_oracle_mix",
    "check_overfitting_posture",
    "check_placebo_outcomes",
    "check_promotion_hygiene",
    "check_statistical_power",
    "check_weight_revisit",
    "rank_checks_for_report",
    "review_practices",
    "summarize_corpus",
]
