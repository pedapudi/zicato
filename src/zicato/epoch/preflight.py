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
(b) The **scripted-perturbation duel** — the champion vs a deliberately
    degraded copy of itself. The degradation is synthetic and mechanical:
    the FIRST enumerated mutation point has its span blanked/scrambled in
    a scratch copy of the snapshot via the existing applier machinery
    (:func:`zicato.mutation.applier.apply_patches`). The degraded tree is
    **ephemeral** — a temp directory, never registered in the lineage —
    and its single draw caches under the champion's id on a reserved
    replicate index (:data:`PREFLIGHT_REPLICATE_BASE`), so re-running the
    pre-flight is idempotent and can never collide with a real duel's
    cache slots. ``|degraded_scalar - mean(champion_scalars)|`` is the
    contract's demonstrated **achievable signal**.

Verdict
-------

* ``"warn"`` (**saturated**) when the scalar spread across ALL probes —
  every A/A draw plus the degraded draw — is exactly zero: even a
  deliberately-broken tree scores identically to the champion (the
  ``1.000000`` signature).
* ``"refuse"`` (**refuse-recommended**) when the achievable signal is at
  or below the measured noise floor: the contract cannot distinguish a
  real degradation from a re-roll, so it cannot possibly resolve the
  smaller improvements a proposer will offer.
* ``"ok"`` otherwise.

The verdict is **recommend-only** — it never gates a run. It persists
onto the epoch record (``config.json``'s additive ``preflight`` field,
never hashed) and flows into the per-round loop-health report through
:func:`zicato.health.diagnostics.detect_preflight_verdict`. Surfaces:
``zicato board preflight`` (manual) and the opt-in epoch-open hook
(``config.json``: ``"contract_preflight": K``), mirroring how the A/A
noise-floor calibration is wired.
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
    measure_noise_floor,
)

#: Replicate-index base for the pre-flight's degraded draw. Reserved far
#: above both real duel replicates (which count up from 0) and the A/A
#: calibration draws (base 1000), so the pre-flight's cache slot can never
#: collide with — or pre-seed — anything a tournament or audit reads.
PREFLIGHT_REPLICATE_BASE: int = 2000

#: The three pre-flight verdicts, weakest concern first.
VERDICT_OK: str = "ok"
#: Saturation — the scalar spread across every probe is exactly zero.
VERDICT_WARN: str = "warn"
#: The achievable signal is at or below the measured A/A noise floor.
VERDICT_REFUSE: str = "refuse"


@dataclass(frozen=True, slots=True)
class PreflightReport:
    """One contract pre-flight measurement for one epoch.

    Fields
    ------
    epoch_id, generation_id:
        The epoch (contract) probed and the champion generation that
        duelled its own degraded copy.
    verdict:
        ``"ok"`` | ``"warn"`` (saturated) | ``"refuse"`` (signal at or
        below the noise floor). Recommend-only, never a gate.
    noise_floor_max_abs_delta, noise_floor_runs:
        The measured A/A floor (``max |delta_scalar|`` over K draws) and
        the K it was measured with.
    champion_scalars:
        The K per-draw A/A scalars of the champion, in draw order.
    degraded_scalar:
        The aggregate scalar of the deliberately-degraded copy.
    signal:
        ``|degraded_scalar - mean(champion_scalars)|`` — the contract's
        demonstrated achievable signal.
    degraded_mutation_id, degraded_mutation_kind, degraded_file:
        Which mutation point the synthetic degradation targeted (the
        FIRST enumerated point), its kind, and the file it lives in —
        so an operator can judge whether the probe was representative.
    measured_at:
        ISO-8601 UTC timestamp of the measurement.
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
            "contract pre-flight: deliberately degrade the first enumerated "
            "mutation point to measure the contract's achievable signal"
        ),
    )


def preflight_verdict(
    champion_scalars: tuple[float, ...] | list[float],
    degraded_scalar: float,
    floor_max_abs_delta: float,
) -> tuple[str, float]:
    """``(verdict, signal)`` from the pre-flight's raw scalars. Pure.

    * **Saturation first**: when the spread across ALL probes (every A/A
      draw plus the degraded draw) is exactly zero, even a deliberately-
      broken tree scored identically — the contract cannot discriminate
      anything (the ``1.000000`` signature) ⇒ ``"warn"``. This is checked
      before the floor comparison because a saturated contract trivially
      also has ``signal == floor == 0``, and the saturation diagnosis is
      the actionable one (the probe moved NOTHING, so the board — not the
      noise — is the problem).
    * ``"refuse"`` when the achievable signal is at or below the measured
      floor — an A/A re-roll moves the scalar as much as a deliberate
      degradation does, so duels are decided by noise.
    * ``"ok"`` otherwise.
    """
    scalars = [float(s) for s in champion_scalars]
    signal = abs(float(degraded_scalar) - (sum(scalars) / len(scalars))) if scalars else 0.0
    probes = [*scalars, float(degraded_scalar)]
    spread = max(probes) - min(probes) if probes else 0.0
    if spread == 0.0:
        return VERDICT_WARN, signal
    if signal <= float(floor_max_abs_delta):
        return VERDICT_REFUSE, signal
    return VERDICT_OK, signal


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
) -> tuple[PreflightReport, NoiseFloor]:
    """Measure the contract's noise floor AND achievable signal; verdict.

    Steps (see the module docstring): (a) the A/A floor via
    :func:`~zicato.tournament.calibration.measure_noise_floor` — K fresh
    draws of ``generation``, cache-shared with ``zicato board audit``;
    (b) the scripted-perturbation duel — the FIRST enumerated mutation
    point of the champion's snapshot is degraded in an ephemeral scratch
    copy (the real lineage is never touched) and that copy is scored once
    through the same board-unit runner; (c) the pure
    :func:`preflight_verdict` over the resulting scalars.

    Returns ``(report, floor)``; the caller decides what to persist
    (:func:`zicato.epoch.lifecycle.set_epoch_preflight` /
    :func:`~zicato.epoch.lifecycle.set_epoch_noise_floor`).

    Raises :class:`ValueError` when the champion's snapshot enumerates no
    mutation points — with no mutable surface there is nothing to degrade
    (and nothing for an evolve loop to optimize either).
    """
    from zicato.mutation.applier import apply_patches  # noqa: PLC0415
    from zicato.mutation.enumerator import enumerate_mutations  # noqa: PLC0415
    from zicato.orchestrator import _resolve_mutable_trees  # noqa: PLC0415
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
    )

    # (b) The scripted-perturbation duel. FIRST enumerated point — the
    # enumerator's ordering is deterministic, so the probe is stable
    # across re-runs of the same snapshot.
    points = enumerate_mutations(_resolve_mutable_trees(adapter, generation.snapshot_root))
    if not points:
        raise ValueError(
            f"contract pre-flight: no mutation points enumerated under "
            f"{generation.snapshot_root}; nothing to degrade (and nothing to evolve)"
        )
    point = points[0]
    patch = degraded_patch_for(point)

    stamped_board = _stamp_judge_only(_stamp_disable_drift(board, disable_drift), judge_only)

    with tempfile.TemporaryDirectory(prefix="zicato-preflight-") as scratch:
        degraded_root = Path(scratch) / "degraded"
        # The applier copies the champion snapshot (code-only, run
        # artifacts excluded) and lands the degradation atomically — the
        # real lineage is never touched; the tree lives only inside this
        # ``with`` block.
        apply_patches(generation.snapshot_root, [patch], degraded_root)
        degraded_gen = replace(generation, snapshot_root=degraded_root)
        losses = await _run_board_units_fast(
            adapter=adapter,
            child_gen=degraded_gen,
            # Stamped like the calibration draws: the harness derives any
            # seeded noise from the STAMPED index, so the degraded draw is
            # an independent sample rather than a re-roll of an A/A seed.
            board=_stamp_replicate_index(stamped_board, PREFLIGHT_REPLICATE_BASE),
            weights=weights,
            config=config,
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            match_id="contract-preflight:degraded",
            # Reserved slot: never collides with duels (0..) or the A/A
            # calibration draws (1000..); a re-run is an idempotent HIT.
            replicate_index=PREFLIGHT_REPLICATE_BASE,
        )
        agg = aggregate_generation_score(list(losses.values()), weights)
        degraded_scalar = float(agg.get("scalar", 0.0))

    # (c) Verdict.
    verdict, signal = preflight_verdict(floor.scalars, degraded_scalar, floor.max_abs_delta)
    report = PreflightReport(
        epoch_id=epoch_id,
        generation_id=generation.id,
        verdict=verdict,
        noise_floor_max_abs_delta=floor.max_abs_delta,
        noise_floor_runs=floor.runs,
        champion_scalars=floor.scalars,
        degraded_scalar=degraded_scalar,
        signal=signal,
        degraded_mutation_id=point.id,
        degraded_mutation_kind=str(point.kind),
        degraded_file=str(point.file),
        measured_at=_dt.datetime.now(_dt.UTC).replace(microsecond=0).isoformat(),
    )
    return report, floor


__all__ = [
    "PREFLIGHT_REPLICATE_BASE",
    "VERDICT_OK",
    "VERDICT_REFUSE",
    "VERDICT_WARN",
    "PreflightReport",
    "degraded_content_for",
    "degraded_patch_for",
    "preflight_verdict",
    "run_contract_preflight",
]
