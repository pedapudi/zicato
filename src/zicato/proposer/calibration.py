"""Pure, deterministic sampler for the critic-calibration channel. NO IO.

Closes the loop on the prediction-accuracy grader
(:func:`zicato.tournament.detail.hypothesis_ledger` /
:func:`~zicato.tournament.detail.grade_hypothesis_predictions`, the
``/api/hypothesis-accuracy`` dashboard feed): that grader has scored every
settled hypothesis's predicted-vs-realized movements for the DASHBOARD only —
the proposer never saw its own calibration. This module renders a redacted
per-reign calibration summary back INTO the proposer context so the LLM can see
its own MISS PATTERN and hypothesize more honestly.

The design + the normative redaction contract live in
``docs/design/PROPOSER.md`` §2.8; this module is its mechanical enforcement.
The invariants, mirroring the genealogy (§2.7) / process-exemplar precedents:

* **Envelope-safe by construction.** A :class:`CalibrationClaim` carries a
  proposer-AUTHORED ``core_idea``, a WHOLE-CANDIDATE Δscalar (banded here,
  never rendered raw), and the grader's ``(matches, predictions)`` COUNTS. It
  NEVER carries a board-entry id, a per-entry result, an exact Δscalar, or
  anything holdout-derived — the grader scores whole-candidate MOVEMENT
  aggregates, so there is no per-entry read here at all and no per-entry slice
  to leak (PROPOSER.md §2.8; OVERFITTING.md §11).
* **Banded, reusing the existing vocabulary.** The per-claim realized outcome
  is coarsened through :func:`zicato.proposer.prompts._bucket_scalar_delta`
  — the same ``improved`` / ``flat`` / ``regressed`` three-band vocabulary the
  experiment-memory and genealogy channels render — so no new banding
  primitive and no exact response-surface number reaches the model. The overall
  calibration fraction is the proposer's OWN self-accuracy meta-signal (a
  pooled hit rate over its own predictions), never a board number.
* **Deterministic + budget-capped.** No RNG, no wall clock. Counts are
  order-independent tallies; the recent list sorts by round DOWN then
  generation-id ascending (a TOTAL key), so the same claim set yields a
  byte-identical block in ANY input order — the leakage budget is a stable
  block round over round.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

#: Cap on the ``core_idea`` carried per recent claim. The core idea is
#: proposer-authored (in-envelope) — but budget-capped with a head-only,
#: elision-marked truncation so a pathologically long hypothesis line cannot
#: balloon the rendered block (the genealogy ``_CORE_IDEA_MAX`` discipline,
#: PROPOSER.md §2.8).
_CORE_IDEA_MAX = 240


@dataclass(frozen=True, slots=True)
class CalibrationClaim:
    """One settled reign hypothesis as plain data for the pure sampler.

    Assembled by the orchestrator's IO builder from the reign's durable
    experiment records joined with the prediction-accuracy grader's ledger.
    Carries NO board-entry identity: the outcome is a WHOLE-CANDIDATE Δscalar
    (banded here, never rendered raw) and the grade is derived from the grader's
    ``(matches, predictions)`` COUNTS — no per-movement number, no per-entry
    result.

    Fields
    ------
    generation_id:
        Lineage coordinate — the recent-list tie-break backstop.
    round_index:
        The 0-based evolve round that minted this generation — the recent
        list's most-recent-first sort key.
    core_idea:
        The proposer-authored hypothesis core idea (in-envelope free text).
    scalar_score_delta:
        The whole-candidate signed Δscalar (negative = better), or ``None``
        when unsettled. BANDED here — the exact number never escapes.
    matches / predictions:
        The grader's counts: how many of this hypothesis's falsifiable movement
        predictions VERIFIED, of how many it made. ``predictions == 0`` means
        the hypothesis made no gradeable claims (⇒ ``unresolved``).
    is_placebo:
        ``True`` for a random-baseline calibration arm (never a calibration
        claim — a placebo makes no honest prediction to grade).
    """

    generation_id: str
    round_index: int
    core_idea: str
    scalar_score_delta: float | None
    matches: int
    predictions: int
    is_placebo: bool = False


@dataclass(frozen=True, slots=True)
class CalibrationClaimItem:
    """One rendered recent claim — the proposer's own graded hypothesis.

    ``grade`` is ``"hit"`` or ``"miss"`` (unresolved claims carry no realized
    band to show, so they never enter the recent list — the counts still tally
    them). ``banded_outcome`` is the whole-candidate Δscalar through the
    experiment-memory band vocabulary (``improved`` / ``flat`` / ``regressed``,
    or ``""`` when unsettled). ``core_idea`` is proposer-authored (capped).
    """

    generation_id: str
    core_idea: str
    banded_outcome: str
    grade: str


@dataclass(frozen=True, slots=True)
class CalibrationSummary:
    """The redacted per-reign calibration summary rendered into the prompt.

    ``hit_count`` / ``miss_count`` / ``unresolved_count`` are the per-claim-type
    tallies over the reign's settled hypotheses. ``calibration_fraction`` is
    ``hit / (hit + miss)`` — the fraction of GRADED claims the proposer called
    correctly (the proposer's own self-accuracy meta-signal, ``0.0`` when
    nothing graded). ``recent`` is up to ``k`` most-recent hit/miss claims.
    """

    hit_count: int
    miss_count: int
    unresolved_count: int
    calibration_fraction: float
    recent: tuple[CalibrationClaimItem, ...]


def _natural_gid(gid: str) -> tuple[str, int]:
    """Numeric-aware sort key for a ``v{n}`` generation id.

    ``("v", 2)`` sorts before ``("v", 10)`` where a plain string compare
    would not; a non-conforming id degrades to ``(gid, -1)`` — still a
    total, deterministic order.
    """
    head = gid.rstrip("0123456789")
    tail = gid[len(head) :]
    return (head, int(tail)) if tail.isdigit() else (gid, -1)


def _core_idea(text: str) -> str:
    """Normalize + cap the proposer's core idea (head-only, elided).

    Whitespace is collapsed to one line and the line is head-capped to
    :data:`_CORE_IDEA_MAX` with a trailing ellipsis, so an over-long hypothesis
    line cannot balloon the rendered block (the genealogy ``_core_idea`` cap).
    """
    line = " ".join(text.strip().split())
    if len(line) <= _CORE_IDEA_MAX:
        return line
    return line[: _CORE_IDEA_MAX - 1].rstrip() + "…"


def _band_outcome(delta: float | None) -> str:
    """Band a whole-candidate Δscalar through the experiment-memory vocabulary.

    Reuses :func:`zicato.proposer.prompts._bucket_scalar_delta` (lazy import —
    the render module imports THIS one, so a top-level import would cycle) so
    the exact number never escapes and no new banding primitive is introduced.
    An unsettled candidate (``None``) renders no band.
    """
    if delta is None:
        return ""
    from zicato.proposer.prompts import _bucket_scalar_delta  # noqa: PLC0415

    return _bucket_scalar_delta(delta)


def _grade(claim: CalibrationClaim) -> str:
    """Classify a claim into ``hit`` / ``miss`` / ``unresolved``.

    * ``unresolved`` — no gradeable predictions (``predictions <= 0``).
    * ``hit`` — the proposer made predictions and EVERY one verified
      (``matches >= predictions``; the grader never returns ``matches >
      predictions``, so this is ``matches == predictions``). Strict-all-match
      is deliberate — it rewards conservative, well-earned prediction over
      confident over-claiming (PROPOSER.md §2.8).
    * ``miss`` — predictions made but at least one did not verify.
    """
    if claim.predictions <= 0:
        return "unresolved"
    return "hit" if claim.matches >= claim.predictions else "miss"


def sample_calibration(
    claims: Sequence[CalibrationClaim],
    k: int,
) -> CalibrationSummary | None:
    """Summarize the reign's prediction calibration — counts + recent claims.

    Deterministic and IO-free. Placebo arms are excluded. Each settled
    hypothesis is graded (:func:`_grade`) into hit / miss / unresolved; the
    tallies and the pooled ``hit / (hit + miss)`` fraction are computed over
    ALL graded claims, and up to ``k`` most-recent hit/miss claims (round DOWN,
    then generation-id ascending — a TOTAL key) render into ``recent`` with
    their banded whole-candidate outcome.

    Returns ``None`` — the caller's "omit this section entirely" sentinel —
    at ``k <= 0`` or when there is NO graded history (``hit + miss == 0``).
    A calibration block with no hit or miss claims re-presents no miss
    pattern. So a baseline reign, or one whose settled hypotheses all made no
    falsifiable predictions, renders the same prompt bytes as a reign with
    the channel switched off.
    """
    if k <= 0:
        return None

    live = [c for c in claims if not c.is_placebo]
    if not live:
        return None

    hit_count = 0
    miss_count = 0
    unresolved_count = 0
    graded: list[CalibrationClaim] = []
    for claim in live:
        grade = _grade(claim)
        if grade == "hit":
            hit_count += 1
            graded.append(claim)
        elif grade == "miss":
            miss_count += 1
            graded.append(claim)
        else:
            unresolved_count += 1

    graded_total = hit_count + miss_count
    if graded_total == 0:
        return None

    fraction = hit_count / graded_total

    # Most-recent-first with a TOTAL tie-break (round DOWN, then gid ascending
    # NUMERIC-aware, so v2 sorts before v10 within a same-round tie) — the
    # recent list is byte-identical for a fixed claim set in ANY input order,
    # the determinism / leakage-budget pin.
    graded.sort(key=lambda c: (-c.round_index, _natural_gid(c.generation_id)))
    recent = tuple(
        CalibrationClaimItem(
            generation_id=claim.generation_id,
            core_idea=_core_idea(claim.core_idea),
            banded_outcome=_band_outcome(claim.scalar_score_delta),
            grade=_grade(claim),
        )
        for claim in graded[:k]
    )

    return CalibrationSummary(
        hit_count=hit_count,
        miss_count=miss_count,
        unresolved_count=unresolved_count,
        calibration_fraction=fraction,
        recent=recent,
    )


__all__ = [
    "CalibrationClaim",
    "CalibrationClaimItem",
    "CalibrationSummary",
    "sample_calibration",
]
