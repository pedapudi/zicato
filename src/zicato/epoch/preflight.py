"""Contract pre-flight — prove the board can out-signal its own noise.

Board-reflection v1. Before an epoch burns rounds, two cheap measurements
answer the one question that decides whether an evolve loop can work at
all: **is the contract's achievable signal larger than its noise floor?**
The pre-flight closes the two mirror pathologies at the door:

* **Zero variance / saturation** — a contract that cannot discriminate.
  The historical signature is the ``1.000000`` null run: every probe
  scores identically, so the loop spins forever with nothing to climb.
* **Noise swamping the margin** — the evaluation is so stochastic that a
  deliberate degradation of the champion moves the scalar *less* than an
  A/A re-roll of the same tree does. Every duel is then decided by noise.

Measurements
------------

(a) The **A/A noise floor** — K fresh draws of the champion through the
    same board-unit workers every duel uses
    (:func:`zicato.tournament.calibration.measure_noise_floor`, cache-
    idempotent with ``zicato board audit``).
(b) The **scripted-perturbation duels** — the champion vs deliberately
    degraded copies of itself. Each degradation is synthetic and
    mechanical: one enumerated mutation point has its span
    blanked/scrambled in a scratch copy of the snapshot via the existing
    applier machinery (:func:`zicato.mutation.applier.apply_patches`).
    Each degraded tree is **ephemeral** — a temp directory, never
    registered in the lineage — and its single draw caches under the
    champion's id on a reserved replicate index
    (:data:`PREFLIGHT_REPLICATE_BASE` + the probe ordinal), so re-running
    the pre-flight is idempotent and can never collide with a real duel's
    cache slots. ``max`` over probes of
    ``|degraded_scalar - mean(champion_scalars)|`` is the contract's
    demonstrated **achievable signal**.

Probe selection (issue #106)
----------------------------

A single probe point is not a measurement of the contract — it is a
measurement of that ONE point. A point can be **inert** under the current
contract (the canonical case: a tool description that no longer reaches
the deliverable because a structured-output schema bypasses the tool
call), and because :func:`~zicato.mutation.enumerator.enumerate_mutations`
orders deterministically by ``(source_root, file, line_start, id)`` —
enumeration order carries no information about which points matter — the
first point being inert used to mean a permanent, never-flaky ``refuse``
on a perfectly healthy board.

So the probe exercises a **sample**, not a point (see
:func:`select_probe_points`): points whose degradation is a literal no-op
are skipped without spending a draw, and the rest are sampled
round-robin across their declared ``role`` metadata so the sample spans
the mutable surface (instructions, tool descriptions, code regions)
rather than one corner of it. Probing runs until the measurement can no
longer change the verdict — the first probe whose signal clears both the
noise floor and ``promote_margin`` ends it — so the healthy case still
costs exactly ONE degraded draw and only a contract that looks
unmeasurable pays for more evidence before it is called unmeasurable.
:attr:`~RuntimeConfig.preflight_probe_points` caps the sample;
:attr:`~RuntimeConfig.preflight_probe_mutation_ids` (or
``--degrade-mutation-id``) pins it explicitly.

Verdict
-------

* ``"warn"`` (**saturated**) when the scalar spread across ALL probes —
  every A/A draw plus the best degraded draw — is exactly zero: even a
  deliberately-broken tree scores identically to the champion (the
  ``1.000000`` signature).
* ``"inert"`` when the best probe moved the scalar by EXACTLY nothing
  while the champion's own draws demonstrably did vary. The achievable
  signal is then **unmeasured**, not zero — every point the pre-flight
  degraded happens not to reach the deliverable — which is a different
  diagnosis, with a different fix (pick a representative point), from a
  noise-limited contract. Never a hard refusal: refusing here was the
  false-``REFUSE`` bug of issue #106.
* ``"refuse"`` (**refuse-recommended**) when the achievable signal is
  positive but at or below the measured noise floor: the contract cannot
  distinguish a real degradation from a re-roll, so it cannot possibly
  resolve the smaller improvements a proposer will offer.
* ``"ok"`` otherwise.

The promote-margin window (issue #112)
--------------------------------------

"Can the contract out-signal its noise?" and "is ``promote_margin``
reachable?" are different questions, and a run can pass the first while
being guaranteed null by the second: a margin above the largest
improvement the loop can produce makes every challenger unpromotable, so
the loop spends its whole budget confirming that. The window the loop
needs is ``noise < promote_margin < achievable``;
:func:`preflight_window_verdict` asserts BOTH bounds and names which one
failed, because "margin above achievable" and "margin below the floor"
send an operator to opposite fixes — and ``achievable <= noise`` (an
**empty window**) sends them to neither, since no value of the margin is
defensible on such a board.

The verdict persists onto the epoch record (``config.json``'s additive
``preflight`` field, never hashed) and flows into the per-round
loop-health report through
:func:`zicato.health.diagnostics.detect_preflight_verdict`.

Gating (issue #84). At evolve start the loop measures the pre-flight once
per epoch (idempotent, best-effort) UNLESS the runtime opts out
(:attr:`~zicato.core.runtime.RuntimeConfig.preflight_gate` ``== "off"``),
and acts on :func:`effective_gate_verdict` per the gate mode:

* ``"warn"`` (the DEFAULT) — a refuse-worthy / saturated / inert verdict
  and any window failure are LOUDLY warned and surfaced in every round's
  health report, but the run proceeds (the recommend-only philosophy).
* ``"refuse"`` — additionally raises :class:`PreflightRefusedError` when
  EITHER verdict refuses (signal at/below the floor, or a margin at/above
  the achievable signal), stopping the run *before* it spends rounds. An
  ``"inert"`` verdict never refuses: the probe, not the contract, is what
  came up short.

Surfaces: ``zicato board preflight`` (manual, always recommend-only,
carries ``--degrade-mutation-id``) and the number of A/A draws K from the
epoch-open hook (``config.json``: ``"contract_preflight": K``); absent, K
defaults to :data:`~zicato.tournament.calibration.DEFAULT_CALIBRATION_RUNS`.
"""

from __future__ import annotations

import datetime as _dt
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from zicato.core import BoardEntry, Generation, RuntimeConfig, ScoringWeights
from zicato.core.mutation import MutationPoint, Patch
from zicato.tournament.calibration import (
    DEFAULT_CALIBRATION_RUNS,
    NoiseFloor,
    NoiseFloorInconclusive,
    measure_noise_floor,
)

#: Replicate-index base for the pre-flight's degraded draws. Reserved far
#: above both real duel replicates (which count up from 0) and the A/A
#: calibration draws (base 1000), so the pre-flight's cache slots can never
#: collide with — or pre-seed — anything a tournament or audit reads. Probe
#: ``j`` of the sample draws at ``PREFLIGHT_REPLICATE_BASE + j``: distinct
#: slots per probe (so no probe replays another's cached result) and an
#: idempotent HIT on a re-run, since :func:`select_probe_points` is
#: deterministic.
PREFLIGHT_REPLICATE_BASE: int = 2000

#: Width of the pre-flight's reserved replicate block. The next owner in the
#: ladder is the candidate screen at 3000
#: (:data:`zicato.epoch.screen.SCREEN_REPLICATE_BASE`), so the pre-flight owns
#: ``2000..2999`` and a sample may never grow past this many probes — squatting
#: a neighbour's range would make their idempotence a lie (a re-run
#: ``zicato board audit`` / screen would read the pre-flight's draws as its
#: own). Far above any plausible mutable surface: the presentation target, the
#: largest real harness, enumerates 15 points.
PREFLIGHT_REPLICATE_SPAN: int = 1000

#: The pre-flight verdicts, weakest concern first.
VERDICT_OK: str = "ok"
#: Saturation — the scalar spread across every probe is exactly zero.
VERDICT_WARN: str = "warn"
#: Every probed point was INERT: the best probe moved the scalar by exactly
#: nothing while the champion's own draws did vary, so the achievable signal
#: is unmeasured rather than measured-as-zero (issue #106). Deliberately NOT
#: a refusal — the operator must pick a representative point, not fix a board
#: that may be perfectly healthy.
VERDICT_INERT: str = "inert"
#: The achievable signal is positive but at or below the measured A/A floor.
VERDICT_REFUSE: str = "refuse"

#: ``promote_margin`` window failures (:func:`preflight_window_verdict`), each
#: naming a DIFFERENT operator fix.
#:
#: No margin is defensible: the achievable signal does not clear the noise
#: floor, so below the floor promotions are noise and above it nothing
#: promotes. Tuning the margin is wasted work — the board is the problem.
WINDOW_EMPTY: str = "empty_window"
#: The margin sits at or above the largest improvement the loop demonstrably
#: produces: no challenger can ever be promoted (a guaranteed-null run).
WINDOW_MARGIN_ABOVE_ACHIEVABLE: str = "margin_above_achievable"
#: The margin sits at or below the measured noise floor: promotions cannot be
#: distinguished from re-rolls of the same generation.
WINDOW_MARGIN_BELOW_FLOOR: str = "margin_below_floor"


class PreflightRefusedError(RuntimeError):
    """Raised to STOP an evolve run whose contract cannot out-signal its noise.

    Fired only when the operator opted into the HARD gate
    (:attr:`~zicato.core.runtime.RuntimeConfig.preflight_gate` ``== "refuse"``)
    AND the pre-flight measured a ``refuse`` verdict — the contract's
    achievable signal is at or below its own A/A noise floor, so every duel
    would be decided by noise. The default gate mode (``"warn"``) only warns
    and never raises this; the run continues. Carried up through
    ``evolve_n_rounds`` and reported as a clean stop reason (never a
    traceback), so the operator sees why the run refused *before* rounds burn
    budget.
    """


@dataclass(frozen=True, slots=True)
class ProbedPoint:
    """One mutation point the pre-flight considered, and what it measured.

    Reporting only the winning probe would hide the diagnosis issue #106
    asks for: an operator staring at a ``refuse`` needs to see that point A
    was inert and point B was not, so they can judge whether the sample was
    representative before they believe the verdict.

    Fields
    ------
    mutation_id, kind, file, role:
        The point's identity, granularity, source file, and declared
        ``role`` metadata (``""`` when the point declares none) — ``role``
        is the axis :func:`select_probe_points` samples across.
    degraded_scalar, signal:
        The degraded copy's aggregate scalar and
        ``|degraded_scalar - mean(champion_scalars)|``. Both ``None`` when
        the probe was never run (see :attr:`skipped`).
    skipped:
        ``""`` when the point was actually degraded and drawn; else the
        machine-readable reason it cost no draw: ``"no_op_patch"`` (the
        degradation would have produced byte-identical content — an
        unconditionally inert probe, detectable without spending an
        evaluation) or ``"verdict_settled"`` (an earlier probe already
        cleared every bound, so no further probe could change the verdict).
    """

    mutation_id: str
    kind: str
    file: str
    role: str = ""
    degraded_scalar: float | None = None
    signal: float | None = None
    skipped: str = ""

    def to_json(self) -> dict[str, Any]:
        """The JSON shape persisted inside the pre-flight record."""
        return {
            "mutation_id": self.mutation_id,
            "kind": self.kind,
            "file": self.file,
            "role": self.role,
            "degraded_scalar": self.degraded_scalar,
            "signal": self.signal,
            "skipped": self.skipped,
        }


@dataclass(frozen=True, slots=True)
class PreflightReport:
    """One contract pre-flight measurement for one epoch.

    Fields
    ------
    epoch_id, generation_id:
        The epoch (contract) probed and the champion generation that
        duelled its own degraded copy.
    verdict:
        ``"ok"`` | ``"warn"`` (saturated) | ``"inert"`` (every probe moved
        nothing) | ``"refuse"`` (positive signal at or below the noise
        floor). Recommend-only unless the operator opted into the hard
        gate.
    noise_floor_max_abs_delta, noise_floor_runs:
        The measured A/A floor (``max |delta_scalar|`` over K draws) and
        the K it was measured with.
    champion_scalars:
        The K per-draw A/A scalars of the champion, in draw order.
    degraded_scalar:
        The aggregate scalar of the BEST degraded copy — the probe that
        moved the scalar furthest from the champion's mean.
    signal:
        ``|degraded_scalar - mean(champion_scalars)|`` for that best probe
        — the contract's demonstrated achievable signal.
    degraded_mutation_id, degraded_mutation_kind, degraded_file:
        Which mutation point the BEST probe degraded, its kind, and the
        file it lives in — so an operator can judge whether the probe was
        representative.
    measured_at:
        ISO-8601 UTC timestamp of the measurement.
    probed_points:
        Every point the sample considered, in probe order, with its
        per-point signal or the reason it cost no draw (issue #106). The
        max over these IS :attr:`signal`; the record is what lets an
        operator see an inert point next to a live one instead of only the
        winner. Additive — pre-#106 records carry none.
    promote_margin:
        The contract's ``promote_margin`` at measurement time, kept beside
        the measured signal so the window comparison is auditable from the
        persisted record alone. Additive (``0.0`` on pre-#112 records).
    window_verdict, window_failure:
        The ``noise < margin < achievable`` window verdict and which bound
        failed (one of :data:`WINDOW_EMPTY`,
        :data:`WINDOW_MARGIN_ABOVE_ACHIEVABLE`,
        :data:`WINDOW_MARGIN_BELOW_FLOOR`, or ``None``). See
        :func:`preflight_window_verdict`. Additive — pre-#112 records read
        back as ``"ok"`` / ``None``, i.e. "not checked", which is exactly
        how they behaved.
    recommended_margin:
        A ``promote_margin`` that clears the measured noise on a
        draw-count-stable statistic
        (:func:`~zicato.tournament.calibration.recommended_promote_margin`),
        or ``None`` when the floor measured no noise to clear. Additive.
    """

    epoch_id: str
    generation_id: str
    verdict: str
    noise_floor_max_abs_delta: float
    noise_floor_runs: int
    champion_scalars: tuple[float, ...]
    degraded_scalar: float
    signal: float
    degraded_mutation_id: str
    degraded_mutation_kind: str
    degraded_file: str
    measured_at: str
    probed_points: tuple[ProbedPoint, ...] = ()
    promote_margin: float = 0.0
    window_verdict: str = VERDICT_OK
    window_failure: str | None = None
    recommended_margin: float | None = None

    def drawn_probe_count(self) -> int:
        """How many probes actually spent a board evaluation.

        :attr:`probed_points` also carries the points that cost nothing
        (:attr:`ProbedPoint.skipped` — ``no_op_patch`` / ``verdict_settled``),
        so its length overstates the evidence behind :attr:`signal`. Operator
        prose reports THIS count: "best of 5 probed points" when three of the
        eight were dropped for free would tell the operator the sample was
        broader than the measurement was.
        """
        return sum(1 for p in self.probed_points if not p.skipped)

    def to_json(self) -> dict[str, Any]:
        """The JSON shape persisted onto the epoch record."""
        return {
            "epoch_id": self.epoch_id,
            "generation_id": self.generation_id,
            "verdict": self.verdict,
            "noise_floor_max_abs_delta": self.noise_floor_max_abs_delta,
            "noise_floor_runs": self.noise_floor_runs,
            "champion_scalars": list(self.champion_scalars),
            "degraded_scalar": self.degraded_scalar,
            "signal": self.signal,
            "degraded_mutation_id": self.degraded_mutation_id,
            "degraded_mutation_kind": self.degraded_mutation_kind,
            "degraded_file": self.degraded_file,
            "measured_at": self.measured_at,
            "probed_points": [p.to_json() for p in self.probed_points],
            "promote_margin": self.promote_margin,
            "window_verdict": self.window_verdict,
            "window_failure": self.window_failure,
            "recommended_margin": self.recommended_margin,
        }


def degraded_content_for(point: MutationPoint) -> str:
    """The deterministic *worsening* replacement for one mutation point.

    The degradation must (1) be a pure function of the point (so the
    degraded draw's cache slot stays honest across re-runs), (2) survive
    the applier's post-apply syntax gate, and (3) plausibly change
    behaviour. Per kind:

    * ``"span"`` — the span's content REVERSED character-by-character.
      Reversal preserves length and separator structure while destroying
      every word, so a prompt/policy span becomes meaningless without the
      applier having to guess at semantics. (The applier wraps non-literal
      prose as a collision-proof string literal, so the result always
      parses.) An empty/whitespace span degrades to a fixed garbage token
      instead — reversing nothing would be a no-op probe.
    * ``"code"`` — ``pass`` (the region's control flow is blanked; always
      valid Python at any indent thanks to the applier's re-anchoring).
    * ``"file"`` — a comment-only module for ``.py`` files (blank file —
      parses, exports nothing) and the reversed content otherwise.
    """
    if point.kind == "code":
        return "pass\n"
    if point.kind == "file" and point.file.suffix == ".py":
        return "# degraded by contract pre-flight (synthetic worsening probe)\n"
    if not point.content.strip():
        return "zicato-preflight-degraded"
    return point.content[::-1]


def is_no_op_degradation(point: MutationPoint) -> bool:
    """Whether degrading this point would produce byte-identical content.

    An unconditionally inert probe, detectable for free: a palindromic span
    reverses to itself, and a code region that is already exactly ``pass``
    blanks to itself. Such a point can never demonstrate achievable signal no
    matter how healthy the contract is, so :func:`select_probe_points` drops it
    from the sample rather than spending a board evaluation to learn that
    ``signal == 0``. (Inertness that depends on the CONTRACT — a live span the
    deliverable happens not to read — is invisible here and is what the
    multi-point sample plus :data:`VERDICT_INERT` exist for.)
    """
    return degraded_content_for(point) == point.content


def degraded_patch_for(point: MutationPoint) -> Patch:
    """Build the synthetic worsening :class:`Patch` for one mutation point."""
    return Patch(
        id=uuid4().hex,
        mutation_id=point.id,
        op="replace",
        new_content=degraded_content_for(point),
        new_numeric=None,
        new_enum=None,
        rationale=(
            "contract pre-flight: deliberately degrade one enumerated mutation "
            "point to measure the contract's achievable signal"
        ),
    )


def select_probe_points(
    points: list[MutationPoint],
    *,
    limit: int,
    mutation_ids: tuple[str, ...] | list[str] = (),
) -> tuple[list[MutationPoint], list[ProbedPoint]]:
    """Choose the mutation points to degrade. Pure and deterministic.

    Returns ``(sample, skipped)``: the points to probe in probe order, and the
    :class:`ProbedPoint` records for points dropped without spending a draw.

    ``mutation_ids`` — an explicit pin (``--degrade-mutation-id`` /
    :attr:`~zicato.core.runtime.RuntimeConfig.preflight_probe_mutation_ids`) —
    takes the named points in the order given and ignores ``limit``: an
    operator who names the points has answered the selection question
    themselves. An id that no longer enumerates raises :class:`ValueError`
    rather than silently falling back, because a silent fallback would report
    a verdict measured on points the operator did not choose. A pinned point
    whose degradation is a no-op is still probed, so an explicit pin measures
    exactly what was asked (and reports ``signal == 0`` honestly).

    Absent a pin, the sample is drawn **round-robin across declared roles**.
    :func:`~zicato.mutation.enumerator.enumerate_mutations` orders by
    ``(source_root, file, line_start, id)``, which is deterministic but
    carries no information about which points matter — taking a prefix of it
    samples one corner of one file (issue #106: the whole bug). Grouping by
    the point's ``role`` metadata and interleaving the groups spreads a
    ``limit``-sized sample across the *kinds* of mutable surface the harness
    declares (instructions, tool descriptions, code regions) before it takes a
    second point from any one of them, which is the closest thing to a
    representative sample available without running anything. Points with no
    ``role`` group under their ``kind``, so an unannotated harness still gets
    span/code/file spread. Determinism is preserved throughout: group order is
    first appearance in the enumeration, within-group order is the
    enumeration's, and ties never depend on dict iteration of unordered data.
    """
    if limit < 1:
        raise ValueError(f"pre-flight probe limit must be >= 1, got {limit!r}")

    if mutation_ids:
        by_id = {p.id: p for p in points}
        unknown = [mid for mid in mutation_ids if mid not in by_id]
        if unknown:
            raise ValueError(
                f"contract pre-flight: requested mutation point(s) "
                f"{', '.join(sorted(unknown))} do not enumerate under the "
                f"champion snapshot; available ids: "
                f"{', '.join(p.id for p in points) or '(none)'}"
            )
        # dict.fromkeys de-duplicates while preserving the requested order.
        return [by_id[mid] for mid in dict.fromkeys(mutation_ids)], []

    skipped = [
        ProbedPoint(
            mutation_id=p.id,
            kind=str(p.kind),
            file=str(p.file),
            role=str(p.metadata.get("role", "")),
            skipped="no_op_patch",
        )
        for p in points
        if is_no_op_degradation(p)
    ]
    candidates = [p for p in points if not is_no_op_degradation(p)]

    groups: dict[str, list[MutationPoint]] = {}
    for point in candidates:
        key = str(point.metadata.get("role", "")) or str(point.kind)
        groups.setdefault(key, []).append(point)

    interleaved: list[MutationPoint] = []
    depth = 0
    while len(interleaved) < len(candidates):
        for bucket in groups.values():
            if depth < len(bucket):
                interleaved.append(bucket[depth])
        depth += 1
    return interleaved[:limit], skipped


def preflight_verdict(
    champion_scalars: tuple[float, ...] | list[float],
    degraded_scalar: float,
    floor_max_abs_delta: float,
) -> tuple[str, float]:
    """``(verdict, signal)`` from the pre-flight's raw scalars. Pure.

    ``degraded_scalar`` is the BEST probe's scalar — the one furthest from
    the champion mean. Passing only the best probe loses nothing: if it
    moved the scalar by zero then every probe did, so the saturation test
    below decides identically to one run over the whole probe set.

    * **Saturation first**: when the spread across ALL probes (every A/A
      draw plus the degraded draw) is exactly zero, even a deliberately-
      broken tree scored identically — the contract cannot discriminate
      anything (the ``1.000000`` signature) ⇒ ``"warn"``. This is checked
      before the floor comparison because a saturated contract trivially
      also has ``signal == floor == 0``, and the saturation diagnosis is
      the actionable one (the probe moved NOTHING, so the board — not the
      noise — is the problem).
    * ``"inert"`` when the signal is EXACTLY zero while the champion's own
      draws did vary (issue #106). Two facts hold simultaneously: the
      harness demonstrably can move the scalar, and the degradation moved
      it by nothing at all. That is a statement about the PROBE, not the
      contract — with a stochastic harness, landing exactly on the mean of
      K noisy draws is a measure-zero coincidence, and with a
      deterministic one it means the degraded tree is behaviourally
      identical. Reported apart from ``"refuse"`` because it sends the
      operator to a different fix (choose a representative point) and must
      never hard-stop a run: a healthy board whose probed points happen not
      to reach the deliverable is exactly the false refusal #106 filed.
    * ``"refuse"`` when the achievable signal is positive but at or below
      the measured floor — an A/A re-roll moves the scalar as much as a
      deliberate degradation does, so duels are decided by noise.
    * ``"ok"`` otherwise.
    """
    scalars = [float(s) for s in champion_scalars]
    signal = abs(float(degraded_scalar) - (sum(scalars) / len(scalars))) if scalars else 0.0
    probes = [*scalars, float(degraded_scalar)]
    spread = max(probes) - min(probes) if probes else 0.0
    if spread == 0.0:
        return VERDICT_WARN, signal
    if signal == 0.0:
        return VERDICT_INERT, signal
    if signal <= float(floor_max_abs_delta):
        return VERDICT_REFUSE, signal
    return VERDICT_OK, signal


def preflight_window_verdict(
    noise_floor: float,
    promote_margin: float,
    achievable_signal: float,
) -> tuple[str, str | None]:
    """``(verdict, which_side)`` for ``noise < margin < achievable``. Pure.

    The pre-flight's signal measurement answers the lower bound only. This
    asserts the whole window and names the side that failed, because the two
    sides have opposite fixes and reporting them alike sends operators to
    debug the wrong number (issue #112).

    * :data:`WINDOW_EMPTY` (``"warn"``) — ``achievable <= noise``. The window
      is empty: below the floor promotions are noise, above it nothing
      promotes, so NO value of ``promote_margin`` is defensible. Checked first
      because it invalidates both other diagnoses — an operator told "your
      margin is mis-set" will spend a cycle tuning a number that has no valid
      value. Only ``"warn"`` here, not ``"refuse"``: this is the very
      achievable-vs-noise comparison :func:`preflight_verdict` already renders
      (and the ``preflight_gate`` already acts on), so re-refusing it would
      double-gate one fact. What this branch adds is the *margin* sentence.
    * :data:`WINDOW_MARGIN_ABOVE_ACHIEVABLE` (``"refuse"``) — the margin sits
      at or above the largest improvement any probed SINGLE point produces, so
      barring a compound patch no challenger can be promoted and the run is
      null before it starts. Unreachable by the signal verdict, and the one
      failure whose whole cost is avoidable in seconds; refuse-worthy under the
      opt-in gate. Note the bound's honest reading: the pre-flight degrades one
      point per probe, so ``achievable_signal`` LOWER-BOUNDS what the loop can
      reach — a multi-point patch (and recombination, which unions two
      sub-margin fixes on purpose) can legitimately exceed it. That is why the
      health finding is a warning rather than a critical, and why the hard gate
      here is opt-in rather than the default.
    * :data:`WINDOW_MARGIN_BELOW_FLOOR` (``"warn"``) — the margin is inside
      measured noise: promotions cannot be told from re-rolls. Warn, matching
      how the loop has always treated this (the evidence gate can still hold
      promotions to CI separation, so it is a hazard rather than a certainty).
    * ``("ok", None)`` when the window holds.

    Bounds are inclusive on the failing side (``>=`` / ``<=``): a margin
    exactly AT the achievable signal promotes nothing, and one exactly at the
    floor is indistinguishable from noise.
    """
    floor = float(noise_floor)
    margin = float(promote_margin)
    achievable = float(achievable_signal)
    if achievable <= floor:
        return VERDICT_WARN, WINDOW_EMPTY
    if margin >= achievable:
        return VERDICT_REFUSE, WINDOW_MARGIN_ABOVE_ACHIEVABLE
    if margin <= floor:
        return VERDICT_WARN, WINDOW_MARGIN_BELOW_FLOOR
    return VERDICT_OK, None


def effective_gate_verdict(record: dict[str, Any] | None) -> str | None:
    """The verdict the ``preflight_gate`` acts on, from a persisted record.

    A pre-flight now renders two verdicts — the signal-vs-noise
    :func:`preflight_verdict` and the margin-window
    :func:`preflight_window_verdict` — and either can be refuse-worthy, so the
    gate needs one collapsed answer. Returns :data:`VERDICT_REFUSE` when EITHER
    is a refusal, else the record's own ``verdict`` verbatim (so ``"inert"``
    stays ``"inert"`` and never stops a run), or ``None`` when there is no
    verdict to act on.

    Reads the persisted dict rather than a :class:`PreflightReport` so the
    resumed / later-round path — which re-reads ``config.json`` instead of
    re-measuring — reaches the identical decision as the round that measured.
    Tolerant of pre-#112 records, which carry no ``window_verdict``.
    """
    if not isinstance(record, dict):
        return None
    verdict = str(record.get("verdict") or "")
    if not verdict:
        return None
    if verdict == VERDICT_REFUSE or str(record.get("window_verdict") or "") == VERDICT_REFUSE:
        return VERDICT_REFUSE
    return verdict


async def run_contract_preflight(
    *,
    adapter: Any,
    generation: Generation,
    board: list[BoardEntry],
    weights: ScoringWeights,
    config: RuntimeConfig,
    workspace_root: Path,
    epoch_id: str,
    runs: int = DEFAULT_CALIBRATION_RUNS,
    disable_drift: tuple[Any, ...] = (),
    judge_only: bool = False,
    degrade_mutation_id: str | None = None,
    probe_points: int | None = None,
) -> tuple[PreflightReport, NoiseFloor]:
    """Measure the contract's noise floor AND achievable signal; verdict.

    Steps (see the module docstring): (a) the A/A floor via
    :func:`~zicato.tournament.calibration.measure_noise_floor` — K fresh
    draws of ``generation``, cache-shared with ``zicato board audit``;
    (b) the scripted-perturbation duels — a deterministic, role-diverse
    SAMPLE of the champion snapshot's mutation points
    (:func:`select_probe_points`), each degraded in its own ephemeral
    scratch copy (the real lineage is never touched) and scored once
    through the same board-unit runner, until a probe's signal clears
    every bound the verdict depends on; (c) the pure
    :func:`preflight_verdict` over the best probe's scalars plus
    :func:`preflight_window_verdict` over the achievable-signal /
    ``promote_margin`` / floor window.

    ``degrade_mutation_id`` pins the probe to ONE named mutation point
    (``zicato board preflight --degrade-mutation-id``); absent, the pin
    falls back to :attr:`RuntimeConfig.preflight_probe_mutation_ids` and
    then to the automatic sample. ``probe_points`` caps the automatic
    sample, defaulting to :attr:`RuntimeConfig.preflight_probe_points` —
    a CEILING, not a cost: the loop stops at the first probe that settles
    the verdict, so a healthy contract still spends exactly one degraded
    draw.

    Returns ``(report, floor)``; the caller decides what to persist
    (:func:`zicato.epoch.lifecycle.set_epoch_preflight` /
    :func:`~zicato.epoch.lifecycle.set_epoch_noise_floor`).

    Raises :class:`ValueError` when the champion's snapshot enumerates no
    mutation points — with no mutable surface there is nothing to degrade
    (and nothing for an evolve loop to optimize either) — or when a pinned
    ``degrade_mutation_id`` does not enumerate.
    """
    from zicato.core.loss import is_infra_abort_cause  # noqa: PLC0415
    from zicato.mutation.applier import apply_patches  # noqa: PLC0415
    from zicato.mutation.enumerator import enumerate_mutations  # noqa: PLC0415
    from zicato.orchestrator import _resolve_mutable_trees  # noqa: PLC0415
    from zicato.tournament.calibration import (  # noqa: PLC0415
        recommended_promote_margin,
    )
    from zicato.tournament.scheduling import _run_board_units_fast  # noqa: PLC0415
    from zicato.tournament.scoring import aggregate_generation_score  # noqa: PLC0415
    from zicato.tournament.worker_transport import (  # noqa: PLC0415
        _stamp_disable_drift,
        _stamp_judge_only,
        _stamp_replicate_index,
    )

    # (a) The A/A noise floor — the same measurement `zicato board audit`
    # takes, on the same cache slots (idempotent across the two surfaces).
    floor = await measure_noise_floor(
        adapter=adapter,
        generation=generation,
        board=board,
        weights=weights,
        config=config,
        workspace_root=workspace_root,
        epoch_id=epoch_id,
        runs=runs,
        disable_drift=disable_drift,
        judge_only=judge_only,
        # Void the whole pre-flight rather than persist an outage-derived
        # floor: a transient endpoint outage during the epoch's first round
        # must not poison the floor (and, under the hard gate, falsely
        # disqualify the contract). The caller's ``best_effort`` turns the
        # raised :class:`NoiseFloorInconclusive` into a skip + re-measure next
        # round.
        raise_on_infra_abort=True,
    )

    # (b) The scripted-perturbation duels over a deterministic sample.
    points = enumerate_mutations(_resolve_mutable_trees(adapter, generation.snapshot_root))
    if not points:
        raise ValueError(
            f"contract pre-flight: no mutation points enumerated under "
            f"{generation.snapshot_root}; nothing to degrade (and nothing to evolve)"
        )
    pinned: tuple[str, ...]
    if degrade_mutation_id:
        pinned = (degrade_mutation_id,)
    else:
        pinned = tuple(getattr(config, "preflight_probe_mutation_ids", ()) or ())
    limit = (
        probe_points
        if probe_points is not None
        else int(getattr(config, "preflight_probe_points", 1) or 1)
    )
    sample, probed = select_probe_points(points, limit=limit, mutation_ids=pinned)
    if not sample:
        raise ValueError(
            f"contract pre-flight: all {len(points)} mutation point(s) under "
            f"{generation.snapshot_root} degrade to byte-identical content "
            "(palindromic spans / already-blank code regions), so no probe can "
            "demonstrate achievable signal; the mutable surface needs real "
            "content before the contract can be pre-flighted"
        )
    if len(sample) > PREFLIGHT_REPLICATE_SPAN:
        # Probe j draws at PREFLIGHT_REPLICATE_BASE + j, so a sample wider than
        # the reserved block would squat the candidate screen's range and make
        # ITS idempotence a lie. Refuse rather than silently overlap.
        block_end = PREFLIGHT_REPLICATE_BASE + PREFLIGHT_REPLICATE_SPAN - 1
        raise ValueError(
            f"contract pre-flight: a {len(sample)}-point probe sample exceeds the "
            f"reserved replicate block of {PREFLIGHT_REPLICATE_SPAN} "
            f"({PREFLIGHT_REPLICATE_BASE}..{block_end}); lower "
            "runtime.preflight_probe_points (or shorten "
            "runtime.preflight_probe_mutation_ids)"
        )

    margin = float(getattr(weights, "promote_margin", 0.0))
    # The bound past which more probes cannot change either verdict: a signal
    # clearing BOTH the floor (``preflight_verdict``) and the margin
    # (``preflight_window_verdict``) settles them, so probing on would only
    # spend champion evaluations to refine a number nothing reads. Note it is
    # the MARGIN and not just the floor — short-circuiting at the floor alone
    # would let the reported achievable signal understate the true maximum and
    # spuriously trip the margin-above-achievable branch (issue #112).
    settled_bound = max(float(floor.max_abs_delta), margin)

    stamped_board = _stamp_judge_only(_stamp_disable_drift(board, disable_drift), judge_only)
    champion_mean = sum(floor.scalars) / len(floor.scalars) if floor.scalars else 0.0

    best_point = sample[0]
    best_scalar = champion_mean
    best_signal = -1.0
    for ordinal, point in enumerate(sample):
        patch = degraded_patch_for(point)
        with tempfile.TemporaryDirectory(prefix="zicato-preflight-") as scratch:
            degraded_root = Path(scratch) / "degraded"
            # The applier copies the champion snapshot (code-only, run
            # artifacts excluded) and lands the degradation atomically — the
            # real lineage is never touched; the tree lives only inside this
            # ``with`` block.
            apply_patches(generation.snapshot_root, [patch], degraded_root)
            degraded_gen = replace(generation, snapshot_root=degraded_root)
            # One reserved slot per probe: distinct indices are distinct cache
            # slots, so probe N never replays probe M's draw, and because the
            # sample is deterministic a re-run is an idempotent HIT throughout.
            replicate_index = PREFLIGHT_REPLICATE_BASE + ordinal
            losses = await _run_board_units_fast(
                adapter=adapter,
                child_gen=degraded_gen,
                # Stamped like the calibration draws: the harness derives any
                # seeded noise from the STAMPED index, so the degraded draw is
                # an independent sample rather than a re-roll of an A/A seed.
                board=_stamp_replicate_index(stamped_board, replicate_index),
                weights=weights,
                config=config,
                workspace_root=workspace_root,
                epoch_id=epoch_id,
                match_id=f"contract-preflight:degraded:{point.id}",
                replicate_index=replicate_index,
            )
            # Same discipline as the A/A draws: a degraded-probe infra abort
            # makes the signal un-measurable, not zero — void the pre-flight
            # rather than persist a verdict derived from an outage.
            if any(
                is_infra_abort_cause(getattr(lp, "abort_cause", None)) for lp in losses.values()
            ):
                raise NoiseFloorInconclusive(
                    "contract pre-flight: the degraded-perturbation draw hit an infra "
                    "abort (endpoint outage / worker crash); the achievable-signal "
                    "measurement is inconclusive and must not be persisted."
                )
            agg = aggregate_generation_score(list(losses.values()), weights)
            degraded_scalar = float(agg.get("scalar", 0.0))

        signal = abs(degraded_scalar - champion_mean)
        probed.append(
            ProbedPoint(
                mutation_id=point.id,
                kind=str(point.kind),
                file=str(point.file),
                role=str(point.metadata.get("role", "")),
                degraded_scalar=degraded_scalar,
                signal=signal,
            )
        )
        if signal > best_signal:
            best_point, best_scalar, best_signal = point, degraded_scalar, signal
        if best_signal > settled_bound:
            probed.extend(
                ProbedPoint(
                    mutation_id=rest.id,
                    kind=str(rest.kind),
                    file=str(rest.file),
                    role=str(rest.metadata.get("role", "")),
                    skipped="verdict_settled",
                )
                for rest in sample[ordinal + 1 :]
            )
            break

    # (c) Verdicts — signal-vs-noise, then the promote-margin window.
    verdict, signal = preflight_verdict(floor.scalars, best_scalar, floor.max_abs_delta)
    window_verdict, window_failure = preflight_window_verdict(floor.max_abs_delta, margin, signal)
    report = PreflightReport(
        epoch_id=epoch_id,
        generation_id=generation.id,
        verdict=verdict,
        noise_floor_max_abs_delta=floor.max_abs_delta,
        noise_floor_runs=floor.runs,
        champion_scalars=floor.scalars,
        degraded_scalar=best_scalar,
        signal=signal,
        degraded_mutation_id=best_point.id,
        degraded_mutation_kind=str(best_point.kind),
        degraded_file=str(best_point.file),
        measured_at=_dt.datetime.now(_dt.UTC).replace(microsecond=0).isoformat(),
        probed_points=tuple(probed),
        promote_margin=margin,
        window_verdict=window_verdict,
        window_failure=window_failure,
        recommended_margin=(recommended_promote_margin(scalars=floor.scalars) or None),
    )
    return report, floor


__all__ = [
    "PREFLIGHT_REPLICATE_BASE",
    "PREFLIGHT_REPLICATE_SPAN",
    "VERDICT_INERT",
    "VERDICT_OK",
    "VERDICT_REFUSE",
    "VERDICT_WARN",
    "WINDOW_EMPTY",
    "WINDOW_MARGIN_ABOVE_ACHIEVABLE",
    "WINDOW_MARGIN_BELOW_FLOOR",
    "PreflightRefusedError",
    "PreflightReport",
    "ProbedPoint",
    "degraded_content_for",
    "degraded_patch_for",
    "effective_gate_verdict",
    "is_no_op_degradation",
    "preflight_verdict",
    "preflight_window_verdict",
    "run_contract_preflight",
    "select_probe_points",
]
