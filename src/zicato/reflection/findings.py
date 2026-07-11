"""Pillar 4 — ranked, evidence-linked findings with executable edits.

The end of the pipeline: fold the scorecards + adjudications + the consumed
reliability floor into a ranked list of :class:`Finding` objects, each one
transcript-span-grounded (the operator verifies in seconds) and carrying — when
a mechanical fix exists — a ``proposed_op`` that names a REAL builder op whose
args are VALIDATED against that op's signature at emit time
(:func:`validate_proposed_op`, via :func:`inspect.signature`). No prose-only
recommendation stands in for a payload the builder could apply, and no payload
is emitted that the builder would reject (BOARD-REFLECTION.md verdict 6).

Concrete emitters
-----------------
* **Margin below the noise floor** → ``set_gate {promote_margin: 2.5 ×
  floor_max_abs_delta}`` — promoting on noise; lift the margin clear of the
  floor.
* **Redundant judge** (``redundant_with`` at corr ≈ 1) → ``set_weights
  {per_judge_weights: {judge: 0.0}}`` — the judge carries no independent
  signal; zero its weight. (``remove_judge`` is reserved for pure-cost
  duplicates and surfaced as recommendation TEXT, not an op — zeroing the
  weight is the reversible, slot-coherent edit.)
* **False-fire-heavy judge** (precision < ½) → ``set_weights
  {per_judge_weights: {judge: 0.5}}`` — a down-weight suggestion, evidence-
  linked to the FP pile.
* **Missed-fire pile** (recall < 1, FN present) → recommendation only, but the
  finding NAMES the adjudicated span the judge slept through (no auto-op:
  broadening a criterion is an authoring decision).
* **Untested judge** (never fired) and **ambiguous pile** → recommendation
  only.

Loss-weight FITTING stays a non-goal — no emitter ever proposes fitted weights.
Findings are OPERATOR-ONLY output; nothing here crosses into the proposer.
"""

from __future__ import annotations

import hashlib
import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zicato.reflection.adjudicator import VERDICT_FN, VERDICT_FP, JudgeAdjudication
from zicato.reflection.scorecards import JudgeScorecard

#: Down-weight a false-fire-heavy judge is nudged toward (a starting point the
#: operator tunes, not a fitted value).
FP_DOWNWEIGHT: float = 0.5

#: Multiplier applied to the noise floor to recommend a promote margin clear of
#: it (BOARD-REFLECTION.md §"margin from noise floor": 2–3× the noise SD).
MARGIN_FLOOR_MULTIPLE: float = 2.5

# Severity vocabulary + rank (higher = worse; ranking is descending).
SEVERITY_CRITICAL: str = "critical"
SEVERITY_WARNING: str = "warning"
SEVERITY_INFO: str = "info"
_SEVERITY_RANK = {SEVERITY_CRITICAL: 3, SEVERITY_WARNING: 2, SEVERITY_INFO: 1}


@dataclass(frozen=True, slots=True)
class Finding:
    """One ranked, evidence-linked reflection finding."""

    finding_id: str
    pillar: str
    severity: str
    title: str
    detail: str
    evidence: tuple[dict[str, Any], ...]
    recommendation: str
    proposed_op: dict[str, Any] | None

    def to_json(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "pillar": self.pillar,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "evidence": [dict(e) for e in self.evidence],
            "recommendation": self.recommendation,
            "proposed_op": dict(self.proposed_op) if self.proposed_op is not None else None,
        }


def _op_function(op_name: str) -> Any:
    """Resolve a builder op by name; raise on an unknown op."""
    from zicato.builder import operations as ops  # noqa: PLC0415

    fn = getattr(ops, op_name, None)
    if fn is None or not callable(fn):
        raise ValueError(f"proposed_op names an unknown builder op {op_name!r}")
    return fn


def validate_proposed_op(op_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Validate an op payload against the real op signature; return ``{op, args}``.

    Looks the op up in :mod:`zicato.builder.operations` and reflects its
    signature (:func:`inspect.signature`): every key in ``args`` must be a real
    keyword parameter of the op (the leading ``draft`` receiver excluded). An
    unknown key — or an unknown op name — raises :class:`ValueError` at emit
    time, so a mis-authored emitter fails loudly rather than shipping a payload
    the builder would reject when the operator applies it.
    """
    fn = _op_function(op_name)
    sig = inspect.signature(fn)
    accepts_var_kw = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
    valid = {
        name
        for name, p in sig.parameters.items()
        if name != "draft"
        and p.kind in (inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    }
    unknown = set(args) - valid
    if unknown and not accepts_var_kw:
        raise ValueError(
            f"proposed_op {op_name!r} got unknown arg(s) {sorted(unknown)}; "
            f"valid args are {sorted(valid)}"
        )
    return {"op": op_name, "args": dict(args)}


def _finding_id(pillar: str, subject: str, kind: str) -> str:
    """Content-stable finding id (independent of ranking order).

    Deterministic so ``zicato reflect apply <finding_id>`` resolves the same
    finding across re-derivations of an immutable reflection.
    """
    digest = hashlib.sha256(f"{pillar}|{subject}|{kind}".encode()).hexdigest()[:8]
    return f"find-{digest}"


def _adjudication_ref(
    judge_name: str,
    run_ref: str,
    *,
    workspace_root: Path | None,
    epoch_id: str | None,
    reflection_id: str | None,
) -> str:
    """The path (absolute when a workspace is given, else relative) to a verdict."""
    if workspace_root is not None and epoch_id is not None and reflection_id is not None:
        from zicato.core.workspace import reflection_adjudication_path  # noqa: PLC0415

        return str(
            reflection_adjudication_path(
                workspace_root, epoch_id, reflection_id, judge_name, run_ref
            )
        )
    return f"adjudication/{judge_name}/{run_ref}.json"


def _evidence(
    adjudications: list[JudgeAdjudication],
    judge_name: str,
    verdict: str,
    *,
    workspace_root: Path | None,
    epoch_id: str | None,
    reflection_id: str | None,
) -> tuple[dict[str, Any], ...]:
    """Evidence chips (run_ref + span + verdict path) for a judge's verdict pile."""
    out: list[dict[str, Any]] = []
    for a in adjudications:
        if a.judge_name == judge_name and a.verdict == verdict:
            out.append(
                {
                    "run_ref": a.run_ref,
                    "judge_name": judge_name,
                    "span": a.evidence_span,
                    "adjudication_path": _adjudication_ref(
                        judge_name,
                        a.run_ref,
                        workspace_root=workspace_root,
                        epoch_id=epoch_id,
                        reflection_id=reflection_id,
                    ),
                }
            )
    return tuple(out)


def derive_findings(
    *,
    scorecards: list[JudgeScorecard],
    adjudications: list[JudgeAdjudication],
    promote_margin: float | None = None,
    noise_floor_max_abs_delta: float | None = None,
    workspace_root: Path | None = None,
    epoch_id: str | None = None,
    reflection_id: str | None = None,
) -> list[Finding]:
    """Fold scorecards + adjudications + the floor into ranked findings.

    Returns findings sorted by descending severity (ties keep emission order).
    Every ``proposed_op`` is validated against the real op signature before the
    finding is constructed, so a payload that would not apply never ships.
    """
    findings: list[Finding] = []

    # --- calibration: promote margin below the noise floor -----------------
    # Only when the floor is POSITIVE: a zero (or unmeasured-as-0) floor makes
    # the 2.5x recommendation a useless 0.0, and "margin below a zero floor" is
    # not evidence of promoting on noise — it is an absent measurement.
    if (
        promote_margin is not None
        and noise_floor_max_abs_delta is not None
        and noise_floor_max_abs_delta > 0
        and promote_margin < noise_floor_max_abs_delta
    ):
        recommended = round(MARGIN_FLOOR_MULTIPLE * float(noise_floor_max_abs_delta), 6)
        findings.append(
            Finding(
                finding_id=_finding_id("calibration", "gate", "margin_below_floor"),
                pillar="calibration",
                severity=SEVERITY_CRITICAL,
                title="Promote margin is below the noise floor",
                detail=(
                    f"promote_margin={promote_margin} is below the measured noise floor "
                    f"max_abs_delta={noise_floor_max_abs_delta} — the gate is promoting on "
                    f"measurement noise. Recommend lifting it to {recommended} "
                    f"({MARGIN_FLOOR_MULTIPLE}× the floor)."
                ),
                evidence=(),
                recommendation=f"raise promote_margin to {recommended}",
                proposed_op=validate_proposed_op("set_gate", {"promote_margin": recommended}),
            )
        )

    # --- per-judge findings -------------------------------------------------
    for card in scorecards:
        name = card.judge_name

        if not card.exercised:
            findings.append(
                Finding(
                    finding_id=_finding_id("discrimination", name, "untested"),
                    pillar="discrimination",
                    severity=SEVERITY_WARNING,
                    title=f"Judge {name!r} is untested",
                    detail=(
                        f"Judge {name!r} never fired across the corpus; the kind it guards was "
                        "not exercised, so its precision/recall cannot be validated here."
                    ),
                    evidence=(),
                    recommendation=(
                        "add an entry that exercises this judge's kind before trusting it"
                    ),
                    proposed_op=None,
                )
            )
            continue

        if card.redundant_with:
            partners = ", ".join(r["judge"] for r in card.redundant_with)
            findings.append(
                Finding(
                    finding_id=_finding_id("calibration", name, "redundant"),
                    pillar="calibration",
                    severity=SEVERITY_WARNING,
                    title=f"Judge {name!r} is redundant",
                    detail=(
                        f"Judge {name!r} fires in near-lockstep (corr ≈ 1) with {partners}; it "
                        "adds no independent signal. Zeroing its weight prunes it reversibly "
                        "(remove_judge is reserved for pure-cost duplicates — use the CLI to "
                        "delete it outright)."
                    ),
                    evidence=(),
                    recommendation=f"zero {name!r}'s weight (redundant with {partners})",
                    proposed_op=validate_proposed_op(
                        "set_weights", {"per_judge_weights": {name: 0.0}}
                    ),
                )
            )

        if card.fp > 0 and card.precision is not None and card.precision < 0.5:
            findings.append(
                Finding(
                    finding_id=_finding_id("validity", name, "false_fire_heavy"),
                    pillar="validity",
                    severity=SEVERITY_WARNING,
                    title=f"Judge {name!r} fires falsely",
                    detail=(
                        f"Judge {name!r} has precision {card.precision:.2f} over {card.fp} "
                        "false fires — it penalizes clean transcripts. Down-weighting reduces "
                        "the noise it injects while the criterion is tightened."
                    ),
                    evidence=_evidence(
                        adjudications,
                        name,
                        VERDICT_FP,
                        workspace_root=workspace_root,
                        epoch_id=epoch_id,
                        reflection_id=reflection_id,
                    ),
                    recommendation=f"down-weight {name!r} toward {FP_DOWNWEIGHT} and tighten it",
                    proposed_op=validate_proposed_op(
                        "set_weights", {"per_judge_weights": {name: FP_DOWNWEIGHT}}
                    ),
                )
            )

        if card.fn > 0 and card.recall is not None and card.recall < 1.0:
            findings.append(
                Finding(
                    finding_id=_finding_id("validity", name, "missed_fire"),
                    pillar="validity",
                    severity=SEVERITY_CRITICAL,
                    title=f"Judge {name!r} misses real failures",
                    detail=(
                        f"Judge {name!r} stayed silent on {card.fn} transcript(s) the "
                        f"adjudicator found exhibited its failure (recall {card.recall:.2f}). "
                        "Broadening the criterion is an authoring decision — the missed spans "
                        "are named in the evidence."
                    ),
                    evidence=_evidence(
                        adjudications,
                        name,
                        VERDICT_FN,
                        workspace_root=workspace_root,
                        epoch_id=epoch_id,
                        reflection_id=reflection_id,
                    ),
                    recommendation=f"broaden {name!r} to catch the named missed-fire spans",
                    proposed_op=None,
                )
            )

        if card.ambiguous_pile:
            findings.append(
                Finding(
                    finding_id=_finding_id("validity", name, "ambiguous_pile"),
                    pillar="validity",
                    severity=SEVERITY_INFO,
                    title=f"Judge {name!r} has a large ambiguous pile",
                    detail=(
                        f"{card.ambiguous} of {card.n_decisions} decisions for {name!r} were "
                        "ambiguous — the adjudicator could not decide, which usually means the "
                        "criterion itself is underspecified."
                    ),
                    evidence=(),
                    recommendation=f"tighten {name!r}'s criterion to reduce ambiguity",
                    proposed_op=None,
                )
            )

    findings.sort(key=lambda f: _SEVERITY_RANK.get(f.severity, 0), reverse=True)
    return findings


__all__ = [
    "FP_DOWNWEIGHT",
    "MARGIN_FLOOR_MULTIPLE",
    "SEVERITY_CRITICAL",
    "SEVERITY_INFO",
    "SEVERITY_WARNING",
    "Finding",
    "derive_findings",
    "validate_proposed_op",
]
