"""Contract pre-flight — prove the board can out-signal its own noise.

Board-reflection v1. Before an epoch burns rounds, two cheap measurements
answer the one question that decides whether an evolve loop can work at
all: **is the movement this contract can measure larger than its own
noise floor?**
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
    demonstrated **degradation signal**. The window section below explains
    why that is NOT the same as achievable improvement.

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
  while the champion's own draws demonstrably did vary. The signal is
  then **unmeasured**, not zero, so the fix is to pick a
  representative point rather than to fix the board. Read the bound
  honestly: it requires the degraded scalar to land EXACTLY on the mean
  of the varying champion draws, which is reachable only on a
  **quantized** scoring scale. On a continuous noisy scale it is
  measure-zero, and on a deterministic harness there is no champion
  spread, so the saturation branch above fires first. So this verdict is
  narrow — correct when it fires, but NOT the thing that keeps a healthy
  board off a false ``refuse``. What protects issue #106's board is (1)
  the role-diverse multi-point sample above and (2) the health finding's
  gate-aware severity (see *Gating* below): under the default
  ``preflight_gate="warn"`` a refusal is a WARNING, loud but structurally
  unable to trip the loop's degenerate-health breaker.
* ``"refuse"`` (**refuse-recommended**) when the measured signal is
  positive but at or below the measured noise floor: the contract cannot
  distinguish a real degradation from a re-roll, so it cannot possibly
  resolve the smaller improvements a proposer will offer.
* ``"ok"`` otherwise.

The promote-margin window (issue #112, corrected by #119)
---------------------------------------------------------

"Can the contract out-signal its noise?" and "is ``promote_margin`` set
sanely?" are different questions, and :func:`preflight_window_verdict`
answers the second by placing the margin against the floor and the
measured signal, naming which side it fell outside of — because "margin
above the signal" and "margin below the floor" send an operator to
opposite fixes, and ``signal <= noise`` (an **empty window**) sends them
to neither, since no value of the margin is defensible on such a board.

What the upper comparison is worth (issue #119). The probe measures
``|degraded_scalar - champion_mean|`` — **degradation headroom**, how far
the scalar moves when a mutation point is DESTROYED. That is how much the
champion has left to LOSE. A promotion requires movement the other way,
and the two are unrelated in general: they diverge hardest exactly where
the loop is most often started, since a champion seeded near the failing
end has little left to break (small degradation headroom) and everything
to gain (large improvement headroom). So the measurement is a single-point
LOWER bound on movement and no bound at all on improvement. The window
verdict is therefore reported honestly as a **warning** — a margin above
the measured signal is worth an operator's attention and may well be
unreachable, but the pre-flight has not shown that it is, and it no longer
stops a run under ``preflight_gate="refuse"``. Improvement headroom stays
**unmeasured**: deriving one from the namespace weights (the scalar's
reachable floor is not ``0`` once a namespace carries a negative weight)
is registered, not built.

The holdout's margin (issue #118)
---------------------------------

When the train/holdout split is active the gate applies a SECOND scalar
bound on the smaller holdout slice
(:attr:`~zicato.core.ScoringWeights.holdout_margin`, falling back to
``promote_margin``), and that slice's coarser ``1/N`` quantization can
make it the binding one. :func:`holdout_window_note` renders the
feasibility note — recommend-only prose, never a verdict — so an operator
sees the second bound alongside the first instead of discovering it as a
run of unexplained ``holdout_not_confirmed`` rejections.

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
  The health finding is then a WARNING, never a critical: the loop's
  degenerate-health circuit breaker observes criticals only, so two
  warn-mode rounds carrying a persisted refusal cannot silently become
  the hard stop the operator explicitly declined
  (:func:`zicato.health.diagnostics.detect_preflight_verdict`).
* ``"refuse"`` — additionally raises :class:`PreflightRefusedError` when
  the signal verdict refuses (the measured signal at/below the floor),
  stopping the run *before* it spends rounds. The margin-window verdicts do
  NOT refuse — they compare the margin against numbers that do not bound a
  challenger's reach (see the window section above) — and
  only here is the health finding a CRITICAL (moot for the breaker, since
  the run already stopped at pre-flight). A deterministic
  probe-selection CONFIG error (an unknown pinned mutation id, a probe
  ceiling wider than the reserved replicate block) also refuses under this
  mode: an outage never disqualifies a contract, but an operator typo is
  not an outage, and silently proceeding unprotected is the one outcome a
  ``"refuse"`` operator did not ask for
  (:class:`PreflightConfigError`). An ``"inert"`` verdict never refuses:
  the probe, not the contract, is what came up short.

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
from zicato.mutation.formats import FORMAT_NEUTRAL_CONTENT
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
#: nothing while the champion's own draws did vary, so the signal
#: is unmeasured rather than measured-as-zero. Deliberately NOT a refusal —
#: the operator must pick a representative point, not fix a board that may be
#: perfectly healthy. NARROW by construction: exact equality with the mean of
#: draws that demonstrably vary is reachable only on a QUANTIZED scoring scale
#: (measure-zero on a continuous one; on a deterministic harness the champion
#: draws do not vary, so :data:`VERDICT_WARN` fires first). Additive and
#: correct when it fires, but it is not the guard against issue #106's false
#: refusal — the role-diverse sample and the warn-mode severity of
#: ``preflight_signal_below_floor`` are.
VERDICT_INERT: str = "inert"
#: The measured signal is positive but at or below the measured A/A floor.
VERDICT_REFUSE: str = "refuse"

#: ``promote_margin`` window failures (:func:`preflight_window_verdict`), each
#: naming a DIFFERENT operator fix.
#:
#: No margin is defensible: the measured signal does not clear the noise
#: floor, so the board demonstrates no movement outside its own noise for a
#: margin to sit in. Tuning the margin is wasted work — the board is the
#: problem.
WINDOW_EMPTY: str = "empty_window"
#: The margin sits at or above the measured DEGRADATION headroom — how far the
#: scalar moved when a mutation point was destroyed. A WARNING, never a
#: refusal: what the probe measures is how much there is to LOSE, and the
#: margin has to be cleared by an IMPROVEMENT, which this measurement does not
#: bound in either direction (issue #119). The name is kept for record
#: compatibility with persisted pre-flights.
WINDOW_MARGIN_ABOVE_ACHIEVABLE: str = "margin_above_achievable"
#: The margin sits at or below the measured noise floor: promotions cannot be
#: distinguished from re-rolls of the same generation.
WINDOW_MARGIN_BELOW_FLOOR: str = "margin_below_floor"


class PreflightRefusedError(RuntimeError):
    """Raised to STOP an evolve run whose contract cannot out-signal its noise.

    Fired only when the operator opted into the HARD gate
    (:attr:`~zicato.core.runtime.RuntimeConfig.preflight_gate` ``== "refuse"``)
    AND the pre-flight measured a ``refuse`` verdict — the contract's
    measured signal is at or below its own A/A noise floor, so every duel
    would be decided by noise. The default gate mode (``"warn"``) only warns
    and never raises this; the run continues. Carried up through
    ``evolve_n_rounds`` and reported as a clean stop reason (never a
    traceback), so the operator sees why the run refused *before* rounds burn
    budget.
    """


class PreflightConfigError(ValueError):
    """A DETERMINISTIC operator-config error in the pre-flight's probe selection.

    Raised for an unknown pinned mutation id
    (:attr:`~zicato.core.runtime.RuntimeConfig.preflight_probe_mutation_ids` /
    ``--degrade-mutation-id``) or a probe ceiling the reserved replicate block
    cannot hold — never for anything the endpoint or the harness did.

    A :class:`ValueError` subclass so every existing ``except ValueError``
    handler (and every test asserting one) keeps working; the distinct type
    exists so the evolve-start hook can tell "the infrastructure hiccuped"
    apart from "the operator mistyped a knob". The pre-flight runs under
    ``best_effort``, and swallowing the first kind is exactly right — a
    transient outage must never disqualify a contract. Swallowing the second
    means a ``preflight_gate="refuse"`` run proceeds with NO gate at all
    because of a typo, which is the one outcome that operator ruled out; so
    under ``"refuse"`` this becomes a :class:`PreflightRefusedError`, and under
    ``"warn"`` it stays the loud warning it has always been.
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
        ``|degraded_scalar - mean(champion_scalars)|`` — DEGRADATION
        headroom, persisted under both ``signal`` (the legacy key) and
        ``degradation_signal`` (the honest one). Both ``None`` when the probe
        was never run (see :attr:`skipped`).
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
            # The same number under its honest name (issue #119). Additive:
            # ``signal`` stays so pre-existing readers keep working.
            "degradation_signal": self.signal,
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
        ``|degraded_scalar - mean(champion_scalars)|`` for that best probe —
        the contract's demonstrated DEGRADATION headroom: how far the scalar
        moved when a mutation point was destroyed. A single-point LOWER bound
        on movement, and NOT a bound on how much a challenger can improve
        (issue #119). Persisted twice — under ``signal``, the key every
        existing reader knows, and under ``degradation_signal``, which says
        what it is.
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
        The margin-vs-floor/signal window verdict and which bound
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
    holdout_note:
        Recommend-only prose about the HOLDOUT confirmation's own bounds when
        the train/holdout split is active (:func:`holdout_window_note`), or
        ``None`` when there is no holdout or both bounds are comfortable.
        Never a verdict — the window verdicts above are unaffected by it.
        Additive (issue #118).
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
    holdout_note: str | None = None

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
            # Same number, honest name (issue #119) — what the probe measured
            # is how far the scalar fell when a mutation point was destroyed.
            # ``signal`` is retained verbatim: dashboards, the builder's
            # pre-flight panel and every persisted-record reader key off it.
            "degradation_signal": self.signal,
            "degraded_mutation_id": self.degraded_mutation_id,
            "degraded_mutation_kind": self.degraded_mutation_kind,
            "degraded_file": self.degraded_file,
            "measured_at": self.measured_at,
            "probed_points": [p.to_json() for p in self.probed_points],
            "promote_margin": self.promote_margin,
            "window_verdict": self.window_verdict,
            "window_failure": self.window_failure,
            "recommended_margin": self.recommended_margin,
            "holdout_note": self.holdout_note,
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
      parses, exports nothing); the format-neutral stand-in from
      :data:`~zicato.mutation.formats.FORMAT_NEUTRAL_CONTENT` for the
      formats the applier structurally checks (``.json`` / ``.toml``),
      since reversing a JSON document would fail that gate and turn the
      probe into a rejected patch instead of a measured degradation; and
      the reversed content for everything else.
    """
    if point.kind == "code":
        return "pass\n"
    if point.kind == "file":
        if point.file.suffix == ".py":
            return "# degraded by contract pre-flight (synthetic worsening probe)\n"
        neutral = FORMAT_NEUTRAL_CONTENT.get(point.file.suffix)
        if neutral is not None:
            return neutral
    if not point.content.strip():
        return "zicato-preflight-degraded"
    return point.content[::-1]


def is_no_op_degradation(point: MutationPoint) -> bool:
    """Whether degrading this point would produce byte-identical content.

    An unconditionally inert probe, detectable for free: a palindromic span
    reverses to itself, and a code region that is already exactly ``pass``
    blanks to itself. Such a point can never demonstrate any signal no
    matter how healthy the contract is, so :func:`select_probe_points` drops it
    from the sample rather than spending a board evaluation to learn that
    ``signal == 0``. (Inertness that depends on the CONTRACT — a live span the
    deliverable happens not to read — is invisible here, and it is the
    multi-point role-diverse sample that covers it: such a point usually
    measures a small NON-zero signal rather than exact zero, so it presents as
    a weak probe the sample can out-measure, not as :data:`VERDICT_INERT`.)
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
            "point to measure the contract's signal"
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
    themselves. An id that no longer enumerates raises
    :class:`PreflightConfigError` rather than silently falling back, because a
    silent fallback would report a verdict measured on points the operator did
    not choose. A pinned point whose degradation is a no-op is still probed, so
    an explicit pin measures exactly what was asked (and reports
    ``signal == 0`` honestly).

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

    Both failure modes are :class:`PreflightConfigError` (a
    :class:`ValueError`): they are deterministic operator-config errors, which
    the evolve-start hook must be able to tell apart from an outage.
    """
    if limit < 1:
        raise PreflightConfigError(f"pre-flight probe limit must be >= 1, got {limit!r}")

    if mutation_ids:
        by_id = {p.id: p for p in points}
        unknown = [mid for mid in mutation_ids if mid not in by_id]
        if unknown:
            raise PreflightConfigError(
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
      draws did vary. Two facts hold simultaneously: the harness
      demonstrably can move the scalar, and the degradation moved it by
      nothing at all. That is a statement about the PROBE, not the
      contract, so it is reported apart from ``"refuse"`` (a different
      fix: choose a representative point) and never hard-stops a run.

      Its reach is narrow, and the comment that used to sit here overstated
      it. The two conditions together — champion spread ``> 0`` and the
      degraded scalar EXACTLY at the champion mean — are only jointly
      satisfiable on a **quantized** scoring scale whose attainable values
      include that mean. On a continuous noisy scale hitting the mean has
      probability zero; on a deterministic harness the champion draws do not
      vary at all, so the saturation branch above claims the case first
      (degraded == champion ⇒ spread ``== 0`` ⇒ ``"warn"``). In particular
      this branch is NOT what saves issue #106's healthy board: a live point
      the deliverable merely routes around measures a small non-zero signal
      and lands in ``"refuse"``. The role-diverse sample
      (:func:`select_probe_points`) is what out-measures it, and the
      gate-aware severity of the resulting health finding is what keeps a
      warn-mode run alive while the operator fixes the sample.
    * ``"refuse"`` when the measured signal is positive but at or below
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
    """``(verdict, which_side)`` for the margin against floor and signal. Pure.

    ``achievable_signal`` is the pre-flight's DEGRADATION signal (the parameter
    keeps its name for callers; see :data:`WINDOW_MARGIN_ABOVE_ACHIEVABLE` for
    why the word "achievable" overstated it). This function names which side of
    the floor/signal pair the margin fell outside of, because the two sides
    have opposite fixes and reporting them alike sends operators to debug the
    wrong number (issue #112).

    Every verdict this returns is now a WARNING (issue #119). The floor-based
    refusal — the contract's signal not clearing its own noise — is
    :func:`preflight_verdict`'s to render and the ``preflight_gate``'s to act
    on; this function measures the margin against numbers that do not bound
    what a challenger can achieve, so it may inform an operator but never stop
    their run.

    * :data:`WINDOW_EMPTY` (``"warn"``) — ``signal <= noise``. The board
      demonstrates no movement outside its own noise, so NO value of
      ``promote_margin`` is defensible. Checked first
      because it invalidates both other diagnoses — an operator told "your
      margin is mis-set" will spend a cycle tuning a number that has no valid
      value. Only ``"warn"`` here, not ``"refuse"``: this is the very
      signal-vs-noise comparison :func:`preflight_verdict` already renders
      (and the ``preflight_gate`` already acts on), so re-refusing it would
      double-gate one fact. What this branch adds is the *margin* sentence.
    * :data:`WINDOW_MARGIN_ABOVE_ACHIEVABLE` (``"warn"``) — the margin sits at
      or above the measured DEGRADATION headroom. Read this one carefully; it
      used to claim more than it measures (issue #119). ``degradation_signal``
      is ``|degraded_scalar - champion_mean|``: how far the scalar moved when
      one mutation point was DESTROYED, i.e. how much this champion has left to
      LOSE. A promotion needs movement in the opposite direction, and the two
      quantities are unrelated in general — a champion seeded near the failing
      end has little left to break and everything to gain, so its degradation
      headroom is small while its improvement headroom is large. The
      measurement therefore bounds the loop's reach from NEITHER side: it is a
      single-point LOWER bound on movement (a compound or recombined patch
      exceeds even that), and it is not an upper bound on improvement at all.

      So this branch says "the margin is larger than the only movement we
      measured", which is a real thing to tell an operator and a real reason to
      look at the margin — but it is not evidence the run is null, and it no
      longer refuses. Under the opt-in hard gate it warns like everything else
      in this class. Deriving a true improvement bound from the namespace
      weights (the scalar's reachable floor is NOT 0 once a namespace carries a
      negative weight) is REGISTERED, not built.
    * :data:`WINDOW_MARGIN_BELOW_FLOOR` (``"warn"``) — the margin is inside
      measured noise: promotions cannot be told from re-rolls. Warn, matching
      how the loop has always treated this (the evidence gate can still hold
      promotions to CI separation, so it is a hazard rather than a certainty).
    * ``("ok", None)`` when the window holds.

    Bounds are inclusive on the failing side (``>=`` / ``<=``): a margin
    exactly AT the measured signal exceeds everything the probe saw, and one
    exactly at the floor is indistinguishable from noise.
    """
    floor = float(noise_floor)
    margin = float(promote_margin)
    achievable = float(achievable_signal)
    if achievable <= floor:
        return VERDICT_WARN, WINDOW_EMPTY
    if margin >= achievable:
        return VERDICT_WARN, WINDOW_MARGIN_ABOVE_ACHIEVABLE
    if margin <= floor:
        return VERDICT_WARN, WINDOW_MARGIN_BELOW_FLOOR
    return VERDICT_OK, None


def holdout_window_note(weights: ScoringWeights, holdout_entries: int) -> str | None:
    """Prose about the HOLDOUT's own promotion bound, or ``None``. Pure.

    The margin window above places ``promote_margin`` — the TRAIN bound. When
    the board is split, a promotion must also survive the holdout
    confirmation, which applies its own scalar tolerance
    (:func:`zicato.tournament.gate.effective_holdout_margin`) and its own
    pass-rate rule on a SMALLER slice. A slice of N entries moves its scalar in
    ``1/N`` steps, so the holdout's steps are the coarse ones and its bound can
    be the binding one while the train window looks perfectly healthy — which
    is how issue #118 presented: a run of ``holdout_not_confirmed`` rejections
    with nothing in the pre-flight to explain them.

    This is a WARNING-class note, never a verdict and never a refusal. It
    reports two feasibility facts an operator cannot otherwise see without
    doing the arithmetic:

    * the scalar bound — one holdout entry flipping pass→fail moves the
      holdout scalar by about ``pass_weight / N``, so the holdout margin must
      reach that for the slice's smallest expressible movement to be
      tolerated. (About, not exactly: the estimate assumes a linear pass term
      and no other namespace moving. It is the right order of magnitude for
      the pass-dominated boards where this bites, and it is prose, not a
      threshold anything is compared against.)
    * the pass-rate rule — at
      :attr:`~zicato.core.ScoringWeights.holdout_entry_regression_budget`
      ``== 0`` a single flipped holdout entry rejects at EVERY margin, under
      either scope, because that rule carries only a float-noise tolerance and
      no scalar bound is consulted once it fires. Raising the holdout margin
      cannot fix this one; only the budget can.

    ``None`` when there is no holdout to speak of (``holdout_entries <= 0``:
    the split is disabled or the board was too small), or when both bounds are
    comfortable.
    """
    from zicato.tournament.gate import effective_holdout_margin  # noqa: PLC0415

    if holdout_entries <= 0:
        return None
    margin = effective_holdout_margin(weights)
    step = float(weights.pass_weight) / holdout_entries
    budget = int(weights.holdout_entry_regression_budget)
    notes: list[str] = []
    if weights.pass_rate_monotonicity and budget == 0:
        notes.append(
            f"one holdout entry flipping pass->fail rejects the promotion at EVERY "
            f"margin: pass-rate monotonicity is on with "
            f"holdout_entry_regression_budget=0, and under "
            f"{weights.pass_rate_monotonicity_scope} scope that rule carries only "
            f"its float-noise tolerance. On a {holdout_entries}-entry holdout that "
            "is a zero-tolerance rule on a noisy slice; set "
            "holdout_entry_regression_budget=1 to let the confirmation absorb one "
            "entry, as the confirmation-only doctrine intends"
        )
    if step > margin:
        notes.append(
            f"the holdout margin {margin:.6g} is under the "
            f"{holdout_entries}-entry holdout's own step size (~{step:.6g}, the "
            "scalar movement of a single entry flipping), so the smallest "
            "regression the slice can express already exceeds it; set "
            "holdout_margin (commensurable with promote_margin at "
            "promote_margin x N_train/N_holdout) rather than raising "
            "promote_margin, which would also raise the train bar"
        )
    if not notes:
        return None
    return "holdout confirmation feasibility: " + "; ".join(notes)


def effective_gate_verdict(record: dict[str, Any] | None) -> str | None:
    """The verdict the ``preflight_gate`` acts on, from a persisted record.

    A pre-flight renders two verdicts — the signal-vs-noise
    :func:`preflight_verdict` and the margin-window
    :func:`preflight_window_verdict` — so the gate needs one collapsed answer.
    Returns :data:`VERDICT_REFUSE` when either is a refusal, else the record's
    own ``verdict`` verbatim (so ``"inert"`` stays ``"inert"`` and never stops
    a run), or ``None`` when there is no verdict to act on.

    The one exception is a persisted :data:`WINDOW_MARGIN_ABOVE_ACHIEVABLE`
    refusal. That verdict was demoted to a warning (issue #119) because it
    compares the margin against DEGRADATION headroom, which does not bound
    what a challenger can achieve; records measured before the demotion still
    carry ``"refuse"`` on it, and honouring them would keep hard-stopping runs
    on the finding the fix retracted. So the collapse skips exactly that
    failure and keeps escalating any other window refusal — the window
    function returns none today, and the branch stays so a future one is not
    silently swallowed.

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
    window_refuses = (
        str(record.get("window_verdict") or "") == VERDICT_REFUSE
        and str(record.get("window_failure") or "") != WINDOW_MARGIN_ABOVE_ACHIEVABLE
    )
    if verdict == VERDICT_REFUSE or window_refuses:
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
    """Measure the contract's noise floor AND degradation signal; verdict.

    Steps (see the module docstring): (b0) probe SELECTION — a
    deterministic, role-diverse SAMPLE of the champion snapshot's mutation
    points (:func:`select_probe_points`), validated first because it is
    free and every way it can fail is deterministic, so no draw is spent
    to discover a mistyped knob; (a) the A/A floor via
    :func:`~zicato.tournament.calibration.measure_noise_floor` — K fresh
    draws of ``generation``, cache-shared with ``zicato board audit``;
    (b) the scripted-perturbation duels — each sampled point degraded in
    its own ephemeral scratch copy (the real lineage is never touched) and
    scored once through the same board-unit runner, until a probe's signal
    clears every bound the verdict depends on; (c) the pure
    :func:`preflight_verdict` over the best probe's scalars plus
    :func:`preflight_window_verdict` over the measured-signal /
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
    (and nothing for an evolve loop to optimize either) — or when every
    point degrades to byte-identical content. Raises the
    :class:`PreflightConfigError` subclass for the two OPERATOR-config
    failures (a pinned id that does not enumerate, a probe ceiling wider
    than the reserved replicate block), which the evolve-start hook escalates
    to a refusal under ``preflight_gate="refuse"``. All four are raised
    before any draw is spent.
    """
    from zicato.board.split import rotation_seed, split_board  # noqa: PLC0415
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

    # (b0) Probe SELECTION first, before a single draw is spent. Enumeration
    # and selection are pure filesystem reads; every way they can fail is a
    # deterministic property of the snapshot or of the operator's config, so
    # learning about it costs nothing — whereas learning about it after (a)
    # has burned K champion evaluations charges the operator real budget for a
    # typo. Behaviour is otherwise identical: nothing here reads the floor.
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
            "demonstrate any signal; the mutable surface needs real "
            "content before the contract can be pre-flighted"
        )
    if len(sample) > PREFLIGHT_REPLICATE_SPAN:
        # Probe j draws at PREFLIGHT_REPLICATE_BASE + j, so a sample wider than
        # the reserved block would squat the candidate screen's range and make
        # ITS idempotence a lie. Refuse rather than silently overlap.
        block_end = PREFLIGHT_REPLICATE_BASE + PREFLIGHT_REPLICATE_SPAN - 1
        raise PreflightConfigError(
            f"contract pre-flight: a {len(sample)}-point probe sample exceeds the "
            f"reserved replicate block of {PREFLIGHT_REPLICATE_SPAN} "
            f"({PREFLIGHT_REPLICATE_BASE}..{block_end}); lower "
            "runtime.preflight_probe_points (or shorten "
            "runtime.preflight_probe_mutation_ids)"
        )

    # (a) The A/A noise floor — the same measurement `zicato board audit`
    # takes, on the same cache slots (idempotent across the two surfaces).
    # This is where the pre-flight starts SPENDING: K champion draws.
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

    margin = float(getattr(weights, "promote_margin", 0.0))
    # The bound past which more probes cannot change either verdict: a signal
    # clearing BOTH the floor (``preflight_verdict``) and the margin
    # (``preflight_window_verdict``) settles them, so probing on would only
    # spend champion evaluations to refine a number nothing reads. Note it is
    # the MARGIN and not just the floor — short-circuiting at the floor alone
    # would let the reported signal understate the true maximum and
    # spuriously trip the margin-above-achievable branch (issue #112).
    settled_bound = max(float(floor.max_abs_delta), margin)

    stamped_board = _stamp_judge_only(_stamp_disable_drift(board, disable_drift), judge_only)
    champion_mean = sum(floor.scalars) / len(floor.scalars) if floor.scalars else 0.0

    # (b) The scripted-perturbation duels over the sample chosen in (b0).
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
                    "abort (endpoint outage / worker crash); the signal "
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
    # The holdout's SECOND bound, when the split is active (issue #118). Split
    # the same way the runner's holdout duel does — same config, same rotation
    # seed — so the entry count the note reasons about is the one the gate will
    # actually confirm against. Prose only: it can neither raise nor lower a
    # verdict, and an unsplit board yields None.
    holdout_seed = rotation_seed(weights.overfitting, epoch_id)
    _train_ids, holdout_ids = split_board(board, weights.overfitting, seed=holdout_seed)
    holdout_note = holdout_window_note(weights, len(holdout_ids))
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
        holdout_note=holdout_note,
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
    "PreflightConfigError",
    "PreflightRefusedError",
    "PreflightReport",
    "ProbedPoint",
    "degraded_content_for",
    "degraded_patch_for",
    "effective_gate_verdict",
    "holdout_window_note",
    "is_no_op_degradation",
    "preflight_verdict",
    "preflight_window_verdict",
    "run_contract_preflight",
    "select_probe_points",
]
