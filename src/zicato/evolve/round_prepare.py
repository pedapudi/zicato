"""Calibration, preflight, mutation snapshot, and baseline preparation."""

# ruff: noqa: E402
from __future__ import annotations

import json
import logging
import time  # noqa: F401  — kept as the ``orch.time`` clock seam (see __all__)
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from zicato.core.types import (
    Generation,
)
from zicato.evolve.lifecycle_services import (
    _beat,
    _now_iso,
)
from zicato.util import best_effort

if TYPE_CHECKING:
    from zicato.runtime.heartbeat import HeartbeatBeater

log = logging.getLogger("zicato.orchestrator")

CallLLM = Callable[[str, str, str], Awaitable[str]]

from zicato.evolve.round_baseline import _atomic_write_text
from zicato.evolve.round_reporting import (
    _collect_epoch_health_inputs,
    _epoch_max_generations_per_contract,
    _health_round_report_path,
)


async def _maybe_calibrate_noise_floor(
    *,
    workspace_root: Path,
    epoch_id: str,
    epoch_cfg: Any,
    workspace_config: Any,
    adapter: Any,
    parent_gen: Generation,
    board: list[Any],
    weights: Any,
    config: Any,
    disable_drift: tuple[Any, ...],
    judge_only: bool,
    beater: HeartbeatBeater | None = None,
    round_index: int = 0,
) -> None:
    """Run the opt-in A/A noise-floor calibration once per epoch.

    Fires only when the workspace config carries ``"calibrate_noise_floor": K``
    (K >= 2 draws) AND the epoch record has no measured floor yet, so the
    measurement happens once at epoch open (the first evolve round of a fresh
    epoch) and every later round short-circuits on the persisted record.
    Best-effort by contract — a calibration failure must never abort the round.

    The measurement is SERIAL and front-loaded — K passes over every board
    entry before the round's first duel — so it owns the heartbeat for its
    duration: ``beater`` (when the loop supplies one) is stamped
    :data:`~zicato.tournament.calibration.CALIBRATION_PHASE` plus a live
    ``draws-completed/total`` suffix, and restored to the round phase on the
    way out. Without that stamp the round's own phase stands while nothing
    proposes or duels, which is indistinguishable from a wedged round
    (issue #175). ``round_index`` names the phase to restore.
    """
    raw = workspace_config.get("calibrate_noise_floor")
    if not raw:
        return
    try:
        runs = int(raw)
    except (TypeError, ValueError):
        log.warning("calibrate_noise_floor=%r is not an integer; skipping calibration", raw)
        return
    if runs < 2:
        log.warning("calibrate_noise_floor=%d needs at least 2 draws; skipping calibration", runs)
        return
    if getattr(epoch_cfg, "noise_floor", None) is not None:
        return

    from zicato.epoch.lifecycle import set_epoch_noise_floor  # noqa: PLC0415
    from zicato.tournament.calibration import (  # noqa: PLC0415
        CALIBRATION_PHASE,
        measure_noise_floor,
    )

    # The whole cost is knowable before the first draw: K draws, each a serial
    # pass over every board entry. Named up front so an operator can decide
    # whether to wait rather than inferring the shape from loss files landing
    # on disk.
    log.info(
        "A/A noise-floor calibration for epoch %s: %d draws x %d board entries "
        "= %d board-entry runs, serially, before this round's first duel "
        '(set "calibrate_noise_floor": 0 to skip)',
        epoch_id,
        runs,
        len(board),
        runs * len(board),
    )
    _beat(beater, phase=f"{CALIBRATION_PHASE}:0/{runs}")
    try:
        with best_effort(
            "A/A noise-floor calibration",
            on_error=lambda exc: log.warning("noise-floor calibration skipped: %s", exc),
        ):
            floor = await measure_noise_floor(
                adapter=adapter,
                generation=parent_gen,
                board=board,
                weights=weights,
                config=config,
                workspace_root=workspace_root,
                epoch_id=epoch_id,
                runs=runs,
                disable_drift=disable_drift,
                judge_only=judge_only,
                on_draw=lambda done, total: _beat(
                    beater, phase=f"{CALIBRATION_PHASE}:{done}/{total}"
                ),
            )
            set_epoch_noise_floor(workspace_root, epoch_id, floor.to_json())
            log.info(
                "A/A noise floor measured for epoch %s (%s, %d draws): "
                "max |delta| = %.6g, delta std = %.6g",
                epoch_id,
                parent_gen.id,
                runs,
                floor.max_abs_delta,
                floor.delta_std,
            )
    finally:
        # The round owns the phase again — the same stamp ``evolve_n_rounds``
        # writes when it schedules the round. Restored on the best-effort skip
        # path too, so a failed calibration never leaves the heartbeat parked
        # on a measurement that has stopped.
        _beat(beater, phase=f"evolve_once:round_{round_index}")


async def _maybe_contract_preflight(
    *,
    workspace_root: Path,
    epoch_id: str,
    epoch_cfg: Any,
    workspace_config: Any,
    adapter: Any,
    parent_gen: Generation,
    board: list[Any],
    weights: Any,
    config: Any,
    disable_drift: tuple[Any, ...],
    judge_only: bool,
    beater: HeartbeatBeater | None = None,
    round_index: int = 0,
) -> str | None:
    """Measure the contract pre-flight once per epoch; return the verdict.

    DEFAULT-ON (issue #84): unless the runtime opts out
    (:attr:`~zicato.core.runtime.RuntimeConfig.preflight_gate` ``== "off"``),
    the achievable-signal pre-flight is measured once at evolve start — the
    A/A noise floor AND the champion-vs-degraded-copy signal — and its
    verdict persisted onto the epoch record. Idempotent: a later round (or a
    resume) short-circuits on the persisted record and re-reports its verdict
    without re-measuring. Best-effort by contract — a measurement failure
    never aborts the round.

    The number of A/A draws K is taken from the ``config.json``
    ``"contract_preflight": K`` key when present (K >= 2), else defaults to
    :data:`~zicato.tournament.calibration.DEFAULT_CALIBRATION_RUNS`.

    Like the calibration above it, the measurement is SERIAL and
    front-loaded: K passes over the board plus one per degraded probe, all
    before the round's first duel. It therefore owns the heartbeat for its
    duration: ``beater`` (when the loop supplies one) is stamped
    :data:`~zicato.epoch.preflight.PREFLIGHT_PHASE` plus a live
    ``units-settled/total`` suffix, and restored to the round phase on
    every way out, including a refusal. Without that stamp the round's own
    phase stands over a minutes-long step that has proposed and duelled
    nothing, which is indistinguishable from a wedged round (issue #276).
    ``round_index`` names the phase to restore.

    Returns the GATE verdict
    (:func:`~zicato.epoch.preflight.effective_gate_verdict` — ``"refuse"``
    when either the signal verdict or the ``promote_margin`` window refuses,
    else the signal verdict verbatim) when one is available (freshly measured
    or already persisted), else ``None`` (skipped / measurement failed). The
    caller enforces the gate: ``"warn"`` mode only warns (done here);
    ``"refuse"`` mode raises
    :class:`~zicato.epoch.preflight.PreflightRefusedError` on a ``refuse``
    verdict. ``"inert"`` is never a refusal — it says the probe rather than
    the contract came up short (issue #106). ``zicato board preflight`` is the
    manual surface.

    One failure escapes the best-effort contract: a
    :class:`~zicato.epoch.preflight.PreflightConfigError` (an unknown pinned
    mutation id, a probe ceiling wider than the reserved replicate block)
    under ``gate_mode == "refuse"`` raises
    :class:`~zicato.epoch.preflight.PreflightRefusedError` from here. "An
    outage never disqualifies a contract" is about NONDETERMINISTIC infra; a
    config typo is deterministic operator error, and a refuse-mode run that
    proceeds ungated because a knob was misspelled is the outcome that
    operator explicitly ruled out. Under ``"warn"`` it stays the loud warning
    it has always been.
    """
    from zicato.core.runtime import PREFLIGHT_GATE_DEFAULT  # noqa: PLC0415
    from zicato.tournament.calibration import DEFAULT_CALIBRATION_RUNS  # noqa: PLC0415

    gate_mode = str(
        getattr(config, "preflight_gate", PREFLIGHT_GATE_DEFAULT) or PREFLIGHT_GATE_DEFAULT
    )
    raw = workspace_config.get("contract_preflight")
    # Resolve K: an explicit (valid) ``contract_preflight`` key wins; else the
    # calibration default. A malformed / too-small key disables the measurement.
    runs: int | None
    if raw:
        try:
            runs = int(raw)
        except (TypeError, ValueError):
            log.warning("contract_preflight=%r is not an integer; skipping pre-flight", raw)
            runs = None
        else:
            if runs < 2:
                log.warning(
                    "contract_preflight=%d needs at least 2 A/A draws; skipping pre-flight", runs
                )
                runs = None
    else:
        runs = DEFAULT_CALIBRATION_RUNS

    # Opted fully out AND no explicit request ⇒ skip entirely. This is the
    # deterministic-oracle escape hatch: no pre-flight is measured at all
    # (issue #84 made the measurement default-on).
    if gate_mode == "off" and not raw:
        return None

    from zicato.epoch.preflight import effective_gate_verdict  # noqa: PLC0415

    # Already measured ⇒ re-report the persisted verdict so the hard gate
    # still applies on a resumed / later round without re-measuring. Collapsed
    # through the SAME helper the fresh path uses, so a resume reaches the
    # identical gate decision as the round that measured.
    existing = getattr(epoch_cfg, "preflight", None)
    if isinstance(existing, dict):
        return effective_gate_verdict(existing)
    if runs is None:
        return None

    from zicato.epoch.lifecycle import (  # noqa: PLC0415
        load_epoch,
        set_epoch_noise_floor,
        set_epoch_preflight,
    )
    from zicato.epoch.preflight import (  # noqa: PLC0415
        PREFLIGHT_PHASE,
        VERDICT_OK,
        PreflightConfigError,
        PreflightRefusedError,
        probe_selection_bounds,
        run_contract_preflight,
    )

    # The whole spend is knowable before the first draw: K A/A draws plus at
    # most one draw per probe the sample may hold, each a serial pass over
    # every board entry. Named up front so an operator can decide whether to
    # wait, rather than inferring the shape from probe draws landing on disk.
    pinned, probe_ceiling = probe_selection_bounds(config)
    probes = len(set(pinned)) or probe_ceiling
    log.info(
        "contract pre-flight for epoch %s: %d A/A draw(s) + up to %d degraded "
        "probe(s) x %d board entries = up to %d board-entry runs, serially, before "
        "this round's first duel (the probe loop stops at the first probe that "
        'settles the verdict; runtime.preflight_gate="off" skips the step)',
        epoch_id,
        runs,
        probes,
        len(board),
        (runs + probes) * len(board),
    )

    verdict_holder: list[str] = []
    # A probe-selection CONFIG error is the one best-effort failure a
    # refuse-mode operator must not have swallowed. ``best_effort`` exists here
    # because an endpoint outage must never disqualify a contract — but a
    # mistyped ``runtime.preflight_probe_mutation_ids`` id is not an outage:
    # it is deterministic, it will fail identically next round, and swallowing
    # it means a run configured to HARD-GATE proceeds with no gate at all
    # because of a typo. Captured here, escalated after the block (raising from
    # inside ``on_error`` would work but hides the control flow).
    config_error: list[PreflightConfigError] = []

    def _on_preflight_error(exc: BaseException) -> None:
        if isinstance(exc, PreflightConfigError):
            config_error.append(exc)
        log.warning("contract pre-flight skipped: %s", exc)

    # The pre-flight owns the heartbeat from here: the round's own phase over
    # a step that has proposed and duelled nothing is the shape a wedged round
    # has. The progress suffix arrives from the measurement's own probe loop.
    _beat(beater, phase=PREFLIGHT_PHASE)
    try:
        with best_effort("contract pre-flight", on_error=_on_preflight_error):
            report, floor = await run_contract_preflight(
                adapter=adapter,
                generation=parent_gen,
                board=board,
                weights=weights,
                config=config,
                workspace_root=workspace_root,
                epoch_id=epoch_id,
                runs=runs,
                disable_drift=disable_drift,
                judge_only=judge_only,
                on_probe=lambda done, total: _beat(
                    beater, phase=f"{PREFLIGHT_PHASE}:{done}/{total}"
                ),
            )
            record = report.to_json()
            set_epoch_preflight(workspace_root, epoch_id, record)
            verdict_holder.append(effective_gate_verdict(record) or report.verdict)
            # The pre-flight's step (a) IS the A/A calibration; persist the
            # floor too when the epoch has none yet (reload — the calibration
            # hook may have written one after ``epoch_cfg`` was loaded), so the
            # margin check + noise-floor detector benefit from the same draws.
            if load_epoch(workspace_root, epoch_id).noise_floor is None:
                set_epoch_noise_floor(workspace_root, epoch_id, floor.to_json())
            if report.verdict == VERDICT_OK and report.window_failure is None:
                log.info(
                    "contract pre-flight OK for epoch %s (%s): degradation signal "
                    "%.6g clears the measured noise floor %.6g and leaves "
                    "promote_margin %.6g inside the window (probed %d point(s))",
                    epoch_id,
                    parent_gen.id,
                    report.signal,
                    report.noise_floor_max_abs_delta,
                    report.promote_margin,
                    report.drawn_probe_count(),
                )
            else:
                # LOUD run-level warning. The diagnosis is per-verdict on purpose:
                # "noise swamps the signal", "the probe was inert", "the margin
                # exceeds what we measured" and "the margin is inside the noise"
                # have four different fixes, and issue #106/#112 both trace operator
                # time wasted to these being reported in the same words. Whether
                # this also STOPS the run is the caller's decision, per
                # ``gate_mode`` — and only the floor-based verdicts can stop it
                # (issue #119).
                log.warning(
                    "CONTRACT PRE-FLIGHT %s — epoch %s (%s): degradation signal "
                    "%.6g vs measured A/A noise floor %.6g vs promote_margin %.6g "
                    "(best of %d probed point(s): %s → scalar %.6g). %s %s See the "
                    "per-round health report / `zicato board preflight`.",
                    (report.window_failure or report.verdict).upper(),
                    epoch_id,
                    parent_gen.id,
                    report.signal,
                    report.noise_floor_max_abs_delta,
                    report.promote_margin,
                    report.drawn_probe_count(),
                    report.degraded_mutation_id,
                    report.degraded_scalar,
                    _preflight_diagnosis(report),
                    (
                        "Set runtime.preflight_gate='refuse' to stop such a run "
                        "before it spends rounds."
                        if gate_mode != "refuse"
                        else "Refusing the run (runtime.preflight_gate='refuse')."
                    ),
                )
        if config_error and gate_mode == "refuse":
            raise PreflightRefusedError(
                f"contract pre-flight CONFIG ERROR for epoch {epoch_id}: "
                f"{config_error[0]}. The pre-flight could not run at all, so the "
                "hard gate has nothing to gate on — refusing the run rather than "
                "spending rounds unprotected (runtime.preflight_gate='refuse'). Fix "
                "the probe-selection config, or set runtime.preflight_gate='warn' to "
                "proceed with the pre-flight skipped."
            )
        return verdict_holder[0] if verdict_holder else None
    finally:
        # The round owns the phase again — the same stamp ``evolve_n_rounds``
        # writes when it schedules the round. Restored on every way out: a
        # settled verdict, a best-effort skip, and the config-error refusal,
        # so the heartbeat never parks on a measurement that has ended.
        _beat(beater, phase=f"evolve_once:round_{round_index}")


def _preflight_diagnosis(report: Any) -> str:
    """The one-sentence "what is wrong and what fixes it" for a pre-flight.

    Kept beside the warning it feeds rather than inside
    :mod:`zicato.epoch.preflight` because it is operator prose for the evolve
    log; the machine-readable equivalents are the verdict constants and the
    health findings (:func:`zicato.health.diagnostics.detect_preflight_verdict`).
    """
    from zicato.epoch.preflight import (  # noqa: PLC0415
        VERDICT_INERT,
        VERDICT_WARN,
        WINDOW_EMPTY,
        WINDOW_MARGIN_ABOVE_ACHIEVABLE,
        WINDOW_MARGIN_BELOW_FLOOR,
    )

    if report.verdict == VERDICT_INERT:
        return (
            "Every probed mutation point left the scalar exactly at the champion "
            "mean while the A/A draws varied, so the signal is "
            "UNMEASURED rather than zero — the probe was inert, which is NOT "
            "evidence against the contract. Pin a point the deliverable "
            "demonstrably depends on via runtime.preflight_probe_mutation_ids "
            "(or widen the sample with runtime.preflight_probe_points)."
        )
    if report.verdict == VERDICT_WARN:
        return (
            "Scalar spread was exactly zero across every probe — even a "
            "deliberately-degraded tree scored identically to the champion, so "
            "the board cannot discriminate candidates at all. Add expectations "
            "or strengthen judges."
        )
    if report.window_failure == WINDOW_MARGIN_ABOVE_ACHIEVABLE:
        return (
            "promote_margin sits at or above the measured DEGRADATION signal — "
            "how far the scalar moved when a mutation point was destroyed, i.e. "
            "how much this champion has left to LOSE. A promotion needs movement "
            "the other way, and the two are unrelated in general (a champion near "
            "the failing end has little left to break and plenty to gain), so "
            "improvement headroom is UNMEASURED and this is NOT evidence the run "
            "is null. Check promote_margin against what a real fix on this board "
            "is worth; it does still need to clear the noise floor, which IS "
            "measured honestly. The probe also degrades ONE point at a time, so "
            "it under-reports even the movement it measures."
        )
    if report.window_failure == WINDOW_MARGIN_BELOW_FLOOR:
        return (
            "promote_margin sits inside the measured noise, so promotions cannot "
            "be told from re-rolls of the same generation. Raise it above the "
            "noise (the record's recommended_margin scales delta_std, which does "
            "not drift with the draw count) and/or keep the evidence gate on."
        )
    if report.window_failure == WINDOW_EMPTY:
        return (
            "The measured signal does not clear the noise floor, so no "
            "promote_margin is defensible on this board — do not tune the "
            "margin. Reduce evaluation noise (more replicates, steadier judges) "
            "or strengthen the board."
        )
    return (
        "Under this contract every duel is decided by noise, so no challenger "
        "can be distinguished from the champion. Strengthen the board / reduce "
        "evaluation noise."
    )


def _warn_margin_below_noise_floor(workspace_root: Path, epoch_id: str) -> None:
    """Log loudly when the contract's margin sits inside measured A/A noise.

    Consulted once per evolve invocation (round 0). A WARNING only when the
    evidence gate is OFF — with the gate on, the defer→replicate loop still
    holds promotions to CI separation, so the margin being inside the noise is
    an informational note rather than a decision hazard. Never hard-refuses.

    Best-effort like the rest of the health path: the :mod:`zicato.health`
    sibling lands in parallel and may be absent, so a missing
    ``zicato.health.inputs`` is silently inert rather than aborting the run.
    """
    try:
        from zicato.health.inputs import epoch_noise_floor_inputs  # noqa: PLC0415
    except ImportError:
        log.debug("zicato.health.inputs unavailable; skipping margin/noise-floor check")
        return
    from zicato.tournament.calibration import (  # noqa: PLC0415
        MARGIN_NOISE_MULTIPLE,
        margin_below_floor,
        recommended_promote_margin_from_floor,
    )

    floor, margin, gate_on = epoch_noise_floor_inputs(workspace_root, epoch_id)
    if margin is None or not margin_below_floor(margin, floor):
        return
    assert isinstance(floor, dict)  # narrowed by margin_below_floor
    max_abs = float(floor.get("max_abs_delta", 0.0))
    # Recommend from the draw-count-STABLE dispersion, never from the range
    # (issue #112): ``max_abs_delta`` grows with K on an unchanged board, so
    # "set the margin above the measured floor" drifts upward as calibration
    # improves — and a campaign followed it straight past the achievable
    # signal, making every duel a rejection by arithmetic.
    recommended = recommended_promote_margin_from_floor(floor) or max_abs
    if gate_on:
        log.info(
            "promote_margin %.6g is below the measured A/A noise floor %.6g "
            "for epoch %s; the evidence gate is ON, so promotions still "
            "replicate to CI separation",
            margin,
            max_abs,
            epoch_id,
        )
        return
    log.warning(
        "promote_margin %.6g is BELOW the measured A/A noise floor %.6g for "
        "epoch %s and the evidence gate is off: duels decided by the margin "
        "alone CANNOT distinguish a real improvement from a re-roll of the "
        "same generation (measured: a naive margin below the floor promotes "
        "pure noise). RECOMMENDED: raise promote_margin to about %.6g — "
        "%.6g sigma of the measured A/A delta_std, a statistic that does NOT "
        "drift upward as calibration draws accumulate the way the max |delta| "
        "range does — and/or enable the evidence gate — "
        '"promote_confidence_threshold": 0.8 with an honest '
        '"promote_confidence_replicates" budget (the scaffolded contracts '
        "use 32) — so promotions must replicate to CI separation. (Floor "
        "measured by `zicato board audit`; this run continues unchanged.)",
        margin,
        max_abs,
        epoch_id,
        recommended,
        MARGIN_NOISE_MULTIPLE,
    )


def _workspace_health_config(workspace_root: Path) -> Any:
    """Resolve the detector thresholds from the workspace ``config.json``.

    The ``health`` block of the workspace config is the operator surface
    for the loop-health thresholds, and the only one: no environment
    variable tunes them. It is parsed by
    :func:`zicato.config.health_config_from_workspace`. Best-effort like
    the rest of the health path: a missing / unreadable / malformed
    workspace config yields ``None`` so ``assess_loop_health`` falls
    back to the defaulted :class:`~zicato.config.HealthConfig` rather
    than blocking the round. (A malformed ``health`` block still fails
    loudly in ``zicato health``, the operator-facing command.)
    """
    try:
        from zicato.config import health_config_from_workspace  # noqa: PLC0415
        from zicato.workspace_loader import load_workspace_config  # noqa: PLC0415

        return health_config_from_workspace(load_workspace_config(workspace_root))
    except Exception as exc:  # noqa: BLE001 — health tuning is best-effort here
        log.debug("workspace health config unavailable (%s); using defaults", exc)
        return None


# _workspace_preflight_gate lives in zicato.health.inputs alongside its
# fellow health-input readers (imported below).


def _assess_and_persist_loop_health(
    workspace_root: Path,
    epoch_id: str,
    round_n: int,
    board: list[Any],
    infra_outage: tuple[int, int] | None = None,
    token_clip: tuple[int, int] | None = None,
    attributable_regressions: dict[str, dict[str, Any]] | None = None,
    on_promote_failure: tuple[str, str, str] | None = None,
) -> tuple[str, bool]:
    """Run the per-round loop-health check and persist its report.

    ``infra_outage`` — the ``(infra_aborted_runs, threshold)`` pair for a
    round the endpoint-outage circuit deferred — is threaded through to
    :func:`~zicato.health.diagnostics.detect_infra_outage`; ``None``
    (every non-deferred round) is inert. ``token_clip`` — the
    ``(tokens_spent, max_tokens_per_round)`` pair for a round the token
    budget clipped — feeds
    :func:`~zicato.health.diagnostics.detect_token_budget_clip` the same
    way, and ``attributable_regressions`` — the per-entry evidence behind a
    PROMOTED duel's ``GateOutcome.attributable_regressions`` — feeds
    :func:`~zicato.health.diagnostics.detect_attributable_entry_regression`.
    ``on_promote_failure`` — the ``(adapter_name, generation_id,
    exception_type)`` triple for a round whose adapter post-promotion hook
    failed — rides the same rail into
    :func:`~zicato.health.diagnostics.detect_on_promote_hook_failed`.

    Calls :func:`zicato.health.diagnostics.assess_loop_health` with the
    epoch's accumulated losses, experiments, and board, then writes the
    resulting :class:`LoopHealth` report atomically to
    ``epochs/{epoch}/health/round_{N}.json``.

    Returns a ``(summary, has_critical)`` tuple:

    * ``summary`` — a one-line human-readable health summary for the
      :class:`EvolveRoundOutcome` (empty when the assessment did not
      run).
    * ``has_critical`` — ``True`` when at least one finding is CRITICAL
      (the loop is producing no signal); the caller logs a prominent
      stderr WARNING in that case, and ``evolve_n_rounds`` feeds it to
      :class:`~zicato.evolve.loop.DegenerateHealthPolicy`, which stops the
      run after two consecutive critical rounds. That second consumer is
      why the pre-flight finding's severity is gate-aware
      (:func:`~zicato.health.inputs.workspace_preflight_gate`): a per-round
      re-emission from a persisted record is an unbroken streak, so grading
      it critical under the default ``"warn"`` gate would stop a run the
      operator asked to let run.

    Best-effort: the :mod:`zicato.health` sibling lands in parallel and
    may be absent. A missing module, or any failure assessing or writing
    the report, is logged at ``debug`` level and yields ``("", False)``
    — the round's outcome is never affected by a health-side error.
    """
    try:
        from zicato.health.diagnostics import assess_loop_health  # noqa: PLC0415
    except ImportError:
        log.debug("zicato.health.diagnostics unavailable; skipping loop-health check")
        return "", False

    try:
        from zicato.health.inputs import (  # noqa: PLC0415
            epoch_noise_floor_inputs,
            epoch_preflight_record,
            epoch_tree_import_gaps,
            workspace_preflight_gate,
        )

        losses_by_generation, experiments = _collect_epoch_health_inputs(
            workspace_root, epoch_id, board
        )
        noise_floor, promote_margin, evidence_gate_on = epoch_noise_floor_inputs(
            workspace_root, epoch_id
        )
        # The runtime-event inputs are passed only when a round actually
        # carries one, so an older / stubbed ``assess_loop_health``
        # signature (the sibling-may-lag tolerance this function already
        # documents) keeps working for every ordinary round.
        extra_kwargs: dict[str, Any] = {}
        if infra_outage is not None:
            extra_kwargs["infra_outage"] = infra_outage
        if token_clip is not None:
            extra_kwargs["token_clip"] = token_clip
        if attributable_regressions:
            extra_kwargs["attributable_regressions"] = attributable_regressions
        if on_promote_failure is not None:
            extra_kwargs["on_promote_failure"] = on_promote_failure
        tree_import_gaps = epoch_tree_import_gaps(workspace_root, epoch_id)
        if tree_import_gaps:
            extra_kwargs["tree_import_gaps"] = tree_import_gaps
        health = assess_loop_health(
            losses_by_generation,
            experiments,
            board,
            epoch_id,
            config=_workspace_health_config(workspace_root),
            max_generations_per_contract=_epoch_max_generations_per_contract(
                workspace_root, epoch_id
            ),
            noise_floor=noise_floor,
            promote_margin=promote_margin,
            evidence_gate_on=evidence_gate_on,
            preflight=epoch_preflight_record(workspace_root, epoch_id),
            preflight_gate=workspace_preflight_gate(workspace_root),
            **extra_kwargs,
        )
    except Exception as exc:  # noqa: BLE001 — health assessment is best-effort
        log.debug("loop-health assessment skipped for %s round %d: %s", epoch_id, round_n, exc)
        return "", False

    summary, has_critical = _summarise_loop_health(health)

    # Promote a "declared judge never fired" finding from a soft, buried
    # health-report entry to a LOUD, operator-visible run-level warning:
    # a judge declared on the board that produced no metric across a whole
    # generation is indistinguishable, to the operator, from one that ran
    # and passed — so it must be surfaced on the terminal rather than only in
    # the round's health JSON (issue #84).
    _warn_dead_judges(epoch_id, round_n, health)

    # Same discipline for the judge that could not answer at all: its zero
    # drift is an error artifact, and it makes the round's scalar better than
    # the truth.
    _warn_erroring_judges(epoch_id, round_n, health)

    # Same discipline for the mutated-tree alarm: a generation whose units never
    # imported a mutable tree scored code the loop never changed, which is
    # indistinguishable — from the terminal — from an honest null result.
    _warn_trees_never_imported(epoch_id, round_n, health)

    with best_effort(
        "loop-health report write",
        on_error=lambda exc: log.debug(
            "loop-health report write skipped for %s round %d: %s", epoch_id, round_n, exc
        ),
    ):
        report_path = _health_round_report_path(workspace_root, epoch_id, round_n)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(report_path, _loop_health_to_json(health, epoch_id, round_n))

    return summary, has_critical


#: Longest ``detail["recommendation"]`` rendered inline on the one-line
#: health summary; longer remediations are clipped with an ellipsis and
#: read in full from the round's health JSON.
_HEALTH_RECOMMENDATION_CLIP = 160


def _summarise_loop_health(health: Any) -> tuple[str, bool]:
    """Derive a one-line summary + critical flag from a ``LoopHealth`` object.

    Tolerant of the sibling's exact :class:`LoopHealth` shape: it is
    documented to expose ``.findings`` and ``.healthy``, and each finding
    is expected to carry a ``code``, a ``severity`` (string) and a
    ``message`` / ``summary`` / ``detail`` text field. Anything missing is
    filled in defensively so a schema drift in the sibling never raises
    here.

    The line names the finding's stable ``code``, its measured summary, and
    — when the detector wrote one — the ``detail["recommendation"]`` saying
    what to change. The recommendation has to be read out of ``detail``
    explicitly: the text walker below accepts only *string* attributes, and
    ``detail`` is a dict, so a walker-only line would carry the remediation
    that fifteen of the nineteen detectors compose no further than the
    round's health JSON (issue #129). Still one line — the clip keeps it
    that way.
    """
    findings = list(getattr(health, "findings", ()) or ())
    healthy = bool(getattr(health, "healthy", not findings))

    def _severity(f: Any) -> str:
        return str(getattr(f, "severity", "") or "").upper()

    critical = [f for f in findings if _severity(f) == "CRITICAL"]
    has_critical = bool(critical)

    if not findings:
        return ("loop healthy" if healthy else "loop health: no findings"), False

    def _text(f: Any) -> str:
        for attr in ("message", "summary", "detail", "description"):
            val = getattr(f, attr, None)
            if isinstance(val, str) and val.strip():
                return val.strip()
        return str(f)

    def _head(f: Any) -> str:
        code = str(getattr(f, "code", "") or "").strip()
        line = f"[{code}] {_text(f)}" if code else _text(f)
        detail = getattr(f, "detail", None)
        rec = detail.get("recommendation") if isinstance(detail, dict) else None
        if isinstance(rec, str) and rec.strip():
            rec = rec.strip()
            if len(rec) > _HEALTH_RECOMMENDATION_CLIP:
                rec = rec[: _HEALTH_RECOMMENDATION_CLIP - 1].rstrip() + "…"
            line = f"{line} — recommended: {rec}"
        return line

    if has_critical:
        head = _head(critical[0])
        extra = f" (+{len(critical) - 1} more critical)" if len(critical) > 1 else ""
        return f"CRITICAL: {head}{extra}", True

    head = _head(findings[0])
    extra = f" (+{len(findings) - 1} more)" if len(findings) > 1 else ""
    return f"{len(findings)} finding(s): {head}{extra}", False


def _loop_health_to_json(health: Any, epoch_id: str, round_n: int) -> str:
    """Serialize a ``LoopHealth`` object to a pretty-printed JSON string.

    Uses :func:`dataclasses.asdict` when the sibling's :class:`LoopHealth`
    is a dataclass; otherwise falls back to reading ``.healthy`` /
    ``.findings`` and coercing each finding via :func:`dataclasses.asdict`
    or ``vars()``. ``epoch_id`` / ``round`` / ``assessed_at`` are stamped
    on so the report is self-describing for the dashboard.
    """
    import dataclasses as _dataclasses  # noqa: PLC0415

    def _coerce(obj: Any) -> Any:
        if _dataclasses.is_dataclass(obj) and not isinstance(obj, type):
            return _dataclasses.asdict(obj)
        if hasattr(obj, "__dict__"):
            return dict(vars(obj))
        return obj

    body: dict[str, Any]
    if _dataclasses.is_dataclass(health) and not isinstance(health, type):
        body = _dataclasses.asdict(health)
    else:
        body = {
            "healthy": bool(getattr(health, "healthy", False)),
            "findings": [_coerce(f) for f in getattr(health, "findings", ()) or ()],
        }
    summary, has_critical = _summarise_loop_health(health)
    body.update(
        {
            "epoch_id": epoch_id,
            "round": round_n,
            "assessed_at": _now_iso(),
            "summary": summary,
            "has_critical": has_critical,
        }
    )
    return json.dumps(body, default=str, indent=2, sort_keys=True) + "\n"


def _warn_loop_no_signal(epoch_id: str, round_n: int, summary: str) -> None:
    """Emit a prominent stderr WARNING that the evolve loop has no signal.

    Called when a round's loop-health assessment surfaces a CRITICAL
    finding (e.g. degenerate scoring — every generation scoring the same,
    so the tournament can never tell a real improvement from noise). The
    operator must see this: a loop that produces no signal will burn LLM
    calls forever without ever promoting anything meaningful.

    The message goes to both the logger (``warning`` level) and, via the
    logger's default stderr handler, the operator's terminal.
    """
    log.warning(
        "LOOP HEALTH CRITICAL — epoch %s round %d: %s. "
        "The evolve loop is producing no usable signal; inspect the "
        "scoring weights / proposer brief before spending more LLM calls.",
        epoch_id,
        round_n,
        summary or "degenerate scoring",
    )


def _warn_dead_judges(epoch_id: str, round_n: int, health: Any) -> None:
    """Emit a prominent stderr WARNING for any board-declared judge that never fired.

    The ``dead_judge`` loop-health finding (a board-declared process judge
    that produced no ``custom:<name>`` metric across the whole epoch) is a
    recommend-only ``warning`` in the health report — easy to miss in the
    per-round JSON. This lifts it to a run-level operator-facing WARNING on
    the terminal: a declared judge that contributes no metric is either
    mis-wired (the events it keys on are never emitted / the harness never
    invokes it) or its criterion is unreachable, and an operator cannot
    tell it apart from a judge that ran and passed. Fired every round the
    finding is present (idempotent, best-effort).

    Tolerant of the health sibling's exact shape: it scans ``.findings``
    for the stable ``code == "dead_judge"`` and reads the finding's
    ``detail["dead_judges"]`` / ``summary`` defensively, so a schema drift
    never raises here.
    """
    for finding in getattr(health, "findings", ()) or ():
        if str(getattr(finding, "code", "") or "") != "dead_judge":
            continue
        detail = getattr(finding, "detail", None)
        dead = detail.get("dead_judges") if isinstance(detail, dict) else None
        named = ", ".join(repr(str(n)) for n in dead) if isinstance(dead, list | tuple) else ""
        summary = str(getattr(finding, "summary", "") or "")
        log.warning(
            "DECLARED JUDGE NEVER FIRED — epoch %s round %d: %s%s "
            "A judge declared on the board produced no metric across the whole "
            "generation: it is either mis-wired (the events it keys on are never "
            "emitted, or the harness never invokes it) or its criterion is "
            "unreachable — an operator cannot tell it apart from a judge that ran "
            "and passed. Confirm each judge is wired to events that fire and its "
            "criterion is reachable (see zicato-design-judges).",
            epoch_id,
            round_n,
            summary or "a declared judge never fired",
            f" (dead: {named})" if named else "",
        )
        return


def _warn_erroring_judges(epoch_id: str, round_n: int, health: Any) -> None:
    """Emit a prominent stderr WARNING for a board-declared judge that RAISED.

    The sibling of :func:`_warn_dead_judges`, for the failure that is easily
    confused with a dead judge (issue #121). A judge whose callable raised
    produced no verdict at all, but every layer below swallows the exception —
    zicato's judge boundary and goldfive's steerer both catch by hard contract
    — and goldfive emits no event for the empty verdict that results. So the
    round scored with that judge's signal silently missing, and its zero drift
    made the generation look BETTER than the evidence supports. That is a
    result the operator must see on the terminal in the round it happens, not
    in a per-round JSON read afterwards.

    Tolerant of the health sibling's exact shape: it scans ``.findings`` for
    the stable ``code == "judge_erroring"`` and reads the finding's
    ``summary`` / ``detail`` defensively, so a schema drift never raises here.
    """
    for finding in getattr(health, "findings", ()) or ():
        if str(getattr(finding, "code", "") or "") != "judge_erroring":
            continue
        detail = getattr(finding, "detail", None)
        recommendation = detail.get("recommendation") if isinstance(detail, dict) else None
        summary = str(getattr(finding, "summary", "") or "")
        log.warning(
            "DECLARED JUDGE RAISED — epoch %s round %d: %s %s",
            epoch_id,
            round_n,
            summary or "a declared judge failed on every invocation",
            str(recommendation)
            or (
                "a judge that raised did not decide anything: its silence lowered "
                "this round's drift loss without evidence. Check the judge / "
                "auxiliary endpoint and model config."
            ),
        )
        return


def _warn_trees_never_imported(epoch_id: str, round_n: int, health: Any) -> None:
    """Emit a prominent stderr WARNING for a tree no unit of a generation imported.

    The ``tree_never_imported`` finding says the loop mutated code that was
    never executed: the board scored, the gate fired and the round promoted or
    rejected on a comparison between two identical unmutated trees. Buried in
    the per-round health JSON it reads like any other warning, and an operator
    cannot tell "the mutations did not help" from "the mutations were never
    under test" — the same argument that lifted ``dead_judge`` to the terminal
    (:func:`_warn_dead_judges`). Fired every round the finding is present
    (idempotent, best-effort), and tolerant of the health sibling's exact shape.
    """
    for finding in getattr(health, "findings", ()) or ():
        if str(getattr(finding, "code", "") or "") != "tree_never_imported":
            continue
        log.warning(
            "MUTATED TREE NEVER IMPORTED — epoch %s round %d: %s. The round's "
            "verdict compared two IDENTICAL unmutated trees, so it carries no "
            "optimization signal. Check that the harness entrypoint imports the "
            "mutable tree rather than an installed copy under another name, and "
            "that the board exercises the code path the mutations target; the "
            "per-generation record is generations/<gen>/harness_load.json "
            "(issue #110).",
            epoch_id,
            round_n,
            str(getattr(finding, "summary", "") or "a mutable tree was never imported"),
        )
