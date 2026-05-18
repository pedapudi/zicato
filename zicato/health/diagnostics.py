"""Detectors and the :class:`LoopHealth` report for evolve-loop health.

The evolve loop's job is to climb a loss surface. When the surface is
flat — every generation scores the same, no drift fires, no expectation
discriminates the candidates — the loop spins for hours and produces
zero optimization signal. An operator should not have to discover that
by inspecting a journal; this module detects it.

Each detector is a pure ``(...) -> list[HealthFinding]`` function over
already-loaded data: per-generation loss profiles, resolved experiment
records, and the epoch's board. :func:`assess_loop_health` runs every
detector and collects the findings into a :class:`LoopHealth` report.

Thresholds
----------

Every detector's tuning knob is a typed field of
:class:`zicato.config.HealthConfig`, with a default and a documented
meaning. An operator re-tunes between runs by setting the matching
``ZICATO_HEALTH_*`` environment variable (read once by
:func:`zicato.config.load_config`) or, when embedding zicato, by
constructing a :class:`~zicato.config.HealthConfig` directly. This
module never reads ``os.environ`` itself — every detector takes the
config as an optional parameter.

================================  =========================  =========
HealthConfig field                Environment variable        Default
================================  =========================  =========
``scoring_window``                ``ZICATO_HEALTH_SCORING_WINDOW``       3
``scoring_epsilon``               ``ZICATO_HEALTH_SCORING_EPSILON``   1e-6
``no_expectations_fraction``      ``ZICATO_HEALTH_NO_EXPECTATIONS_FRACTION``   0.5
``stalled_rejects``               ``ZICATO_HEALTH_STALLED_REJECTS``      3
================================  =========================  =========

* ``scoring_window`` — how many of the most-recent tournaments
  :func:`detect_degenerate_scoring` inspects. The detector fires only
  when *all* tournaments in the window are flat.
* ``scoring_epsilon`` — the absolute ``scalar_score_delta`` below which
  a tournament counts as "produced no signal".
* ``no_expectations_fraction`` — :func:`detect_no_expectations` fires
  when the fraction of board entries lacking an expectation is strictly
  greater than this value.
* ``stalled_rejects`` — how many consecutive ``rejected`` generations
  :func:`detect_stalled_loop` treats as a stall.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any

from zicato.config import HealthConfig, load_config
from zicato.core.types import BoardEntry, LossProfile

# ---------------------------------------------------------------------------
# Tunable thresholds
# ---------------------------------------------------------------------------
#
# The detector thresholds now live as typed fields on
# :class:`zicato.config.HealthConfig`. The module-level constants below
# are kept as named defaults — they mirror that dataclass's field
# defaults and remain importable for the tests and call sites that
# reference them by name.

#: Number of most-recent tournaments :func:`detect_degenerate_scoring`
#: inspects. See :attr:`zicato.config.HealthConfig.scoring_window`.
DEGENERATE_SCORING_WINDOW: int = HealthConfig().scoring_window

#: Absolute ``scalar_score_delta`` below which a tournament counts as
#: producing no optimization signal. See
#: :attr:`zicato.config.HealthConfig.scoring_epsilon`.
DEGENERATE_SCORING_EPSILON: float = HealthConfig().scoring_epsilon

#: Fraction-of-board-entries-without-an-expectation threshold for
#: :func:`detect_no_expectations`. See
#: :attr:`zicato.config.HealthConfig.no_expectations_fraction`.
NO_EXPECTATIONS_FRACTION: float = HealthConfig().no_expectations_fraction

#: Consecutive ``rejected`` generations :func:`detect_stalled_loop`
#: treats as a stall. See
#: :attr:`zicato.config.HealthConfig.stalled_rejects`.
STALLED_LOOP_REJECTS: int = HealthConfig().stalled_rejects

#: Namespace prefix of drift-derived metrics in the unified metric view.
_DRIFT_NAMESPACE = "drift:"


def _resolve_health_config(config: HealthConfig | None) -> HealthConfig:
    """Return ``config`` if given, else load it from the environment once.

    Detectors accept an optional :class:`~zicato.config.HealthConfig` so
    a caller that already holds a loaded config threads it in. When a
    caller passes nothing, :func:`zicato.config.load_config` supplies the
    env-sourced configuration — the single place the environment is
    read.
    """
    if config is not None:
        return config
    return load_config().health


# ---------------------------------------------------------------------------
# Report dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HealthFinding:
    """One diagnostic observation about the evolve loop's health.

    Fields
    ------
    code:
        Stable symbolic identifier for the detector that produced this
        finding. One of ``"degenerate_scoring"``,
        ``"non_differentiating_entry"``, ``"flat_drift_signal"``,
        ``"no_expectations"``, ``"stalled_loop"``.
    severity:
        ``"info"`` | ``"warning"`` | ``"critical"``. A loop is
        considered unhealthy when any ``"warning"`` or ``"critical"``
        finding is present.
    summary:
        One-line human-readable description for terminal output.
    detail:
        Structured specifics — entry ids, generation ids, the numbers
        that tripped the detector. JSON-friendly (str / number / list /
        dict values) so the report round-trips cleanly.
    """

    code: str
    severity: str
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LoopHealth:
    """The aggregate health report for one epoch's evolve loop.

    Fields
    ------
    epoch_id:
        The epoch this report describes.
    findings:
        Every finding produced by every detector, in detector order.
    healthy:
        ``True`` iff no finding has ``"warning"`` or ``"critical"``
        severity. Purely-``"info"`` findings do not flip this to
        ``False`` — they are observations, not problems.
    checked_at:
        ISO-8601 UTC timestamp of when the assessment ran.
    """

    epoch_id: str
    findings: tuple[HealthFinding, ...]
    healthy: bool
    checked_at: str


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------


def _scalar_delta_of(experiment: Any) -> float | None:
    """Return an experiment's tournament ``scalar_score_delta`` if present.

    Accepts both a typed :class:`~zicato.core.types.Experiment` (whose
    ``outcome`` is an :class:`~zicato.core.types.OutcomeRecord` or
    ``None``) and a plain dict shaped like ``experiment.json`` on disk.
    Returns ``None`` when the experiment has not been evaluated yet —
    such experiments carry no scoring signal to judge.
    """
    outcome = _attr_or_key(experiment, "outcome")
    if outcome is None:
        return None
    delta = _attr_or_key(outcome, "scalar_score_delta")
    if delta is None:
        return None
    try:
        return float(delta)
    except (TypeError, ValueError):
        return None


def _attr_or_key(obj: Any, name: str) -> Any:
    """Read ``name`` from ``obj`` whether it is an object or a mapping.

    Detectors accept resolved :class:`~zicato.core.types.Experiment`
    records *or* the raw dicts ``experiment.json`` deserialises to, so
    every field access goes through this shim.
    """
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def detect_degenerate_scoring(
    experiments: list[Any], config: HealthConfig | None = None
) -> list[HealthFinding]:
    """Fire when the last K tournaments all produced no scoring signal.

    Looks at the most-recent ``config.scoring_window`` experiments that
    carry a tournament outcome. When *every* one of them has
    ``|scalar_score_delta|`` below ``config.scoring_epsilon``, the loop
    is spinning with a flat loss surface — there is nothing for the
    proposer to optimize.

    Severity is ``critical``: a degenerate scorer wastes every round's
    wall-clock and the operator must intervene (strengthen the board,
    re-weight scoring, fix the reducer).

    Silent when fewer than ``config.scoring_window`` evaluated
    experiments exist, or when any tournament in the window showed a
    real delta.

    ``config`` defaults to the env-sourced
    :class:`~zicato.config.HealthConfig` via :func:`load_config`.
    """
    health = _resolve_health_config(config)
    window = health.scoring_window
    epsilon = health.scoring_epsilon

    evaluated: list[tuple[Any, float]] = []
    for exp in experiments:
        delta = _scalar_delta_of(exp)
        if delta is not None:
            evaluated.append((exp, delta))

    if len(evaluated) < window:
        return []

    recent = evaluated[-window:]
    if any(abs(delta) > epsilon for _, delta in recent):
        return []

    generation_ids = [str(_attr_or_key(exp, "generation_id") or "") for exp, _ in recent]
    deltas = [delta for _, delta in recent]
    return [
        HealthFinding(
            code="degenerate_scoring",
            severity="critical",
            summary=(
                f"last {window} tournaments produced |Δscalar| ≤ {epsilon:g} — "
                "the loop is spinning with no optimization signal"
            ),
            detail={
                "window": window,
                "epsilon": epsilon,
                "generation_ids": generation_ids,
                "scalar_score_deltas": deltas,
            },
        )
    ]


def detect_non_differentiating_entry(
    losses_by_generation: dict[str, list[LossProfile]],
) -> list[HealthFinding]:
    """Flag board entries whose loss never moves across generations.

    For each board entry, collect its per-generation ``drift_loss``
    across every generation it ran under. When that entry ran under two
    or more generations and produced an *identical* ``drift_loss`` every
    time, it contributes zero discriminating signal — it is a dead test.

    One ``warning`` finding per such entry: the recommended fix is to
    remove the entry or strengthen its expectation so it can actually
    differentiate candidates.

    Entries that ran under only a single generation are ignored — there
    is nothing yet to compare them against.
    """
    # entry_id -> ordered list of (generation_id, drift_loss)
    per_entry: dict[str, list[tuple[str, float]]] = {}
    for generation_id, losses in losses_by_generation.items():
        for loss in losses:
            per_entry.setdefault(loss.entry_id, []).append((generation_id, loss.drift_loss))

    findings: list[HealthFinding] = []
    for entry_id in sorted(per_entry):
        observations = per_entry[entry_id]
        if len(observations) < 2:
            continue
        values = [value for _, value in observations]
        if any(value != values[0] for value in values):
            continue
        generation_ids = sorted({gen for gen, _ in observations})
        findings.append(
            HealthFinding(
                code="non_differentiating_entry",
                severity="warning",
                summary=(
                    f"board entry {entry_id!r} scored an identical drift_loss "
                    f"({values[0]:g}) across all {len(generation_ids)} "
                    "generations it ran under — a dead test"
                ),
                detail={
                    "entry_id": entry_id,
                    "drift_loss": values[0],
                    "generation_ids": generation_ids,
                    "recommendation": (
                        "remove the entry or strengthen its expectation so it "
                        "can differentiate generations"
                    ),
                },
            )
        )
    return findings


def detect_flat_drift_signal(
    losses_by_generation: dict[str, list[LossProfile]],
) -> list[HealthFinding]:
    """Fire when no drift-namespace metric counted anything in the epoch.

    Walks every run's unified metric view and sums the ``count`` of
    every metric in the ``"drift:"`` namespace. When that total is zero
    across the whole epoch, goldfive's drift detectors fired nothing —
    the drift side of the loss is inert and cannot move the scalar.

    Severity is ``warning``: the loop can still optimize on the
    pass/fail side, but half the loss surface is dead.

    Silent when there are no runs at all (nothing to assess).
    """
    total_runs = 0
    drift_total = 0.0
    for losses in losses_by_generation.values():
        for loss in losses:
            total_runs += 1
            for metric in loss.unified_metrics():
                if metric.name.startswith(_DRIFT_NAMESPACE):
                    drift_total += metric.count

    if total_runs == 0:
        return []
    if drift_total > 0.0:
        return []

    return [
        HealthFinding(
            code="flat_drift_signal",
            severity="warning",
            summary=(
                f"zero drift-namespace metric counts across all {total_runs} runs "
                "in the epoch — the drift side of the loss is inert"
            ),
            detail={
                "runs_inspected": total_runs,
                "drift_count_total": drift_total,
                "recommendation": (
                    "confirm goldfive drift detection is wired and the board "
                    "exercises drift-prone behaviour"
                ),
            },
        )
    ]


def detect_no_expectations(
    board_entries: list[BoardEntry], config: HealthConfig | None = None
) -> list[HealthFinding]:
    """Report when most of the board carries no pass/fail expectation.

    Computes the fraction of board entries whose ``expectation`` is
    ``None``. When that fraction is strictly greater than
    ``config.no_expectations_fraction``, the pass/fail side of the loss
    is mostly absent — the loop leans almost entirely on drift loss.

    Severity is ``info``: a drift-only board is a legitimate operator
    choice, but it is worth surfacing because a flat-drift epoch on such
    a board has *no* signal at all.

    Silent on an empty board.

    ``config`` defaults to the env-sourced
    :class:`~zicato.config.HealthConfig` via :func:`load_config`.
    """
    total = len(board_entries)
    if total == 0:
        return []

    threshold = _resolve_health_config(config).no_expectations_fraction
    without = [entry for entry in board_entries if entry.expectation is None]
    fraction = len(without) / total
    if fraction <= threshold:
        return []

    return [
        HealthFinding(
            code="no_expectations",
            severity="info",
            summary=(
                f"{len(without)}/{total} board entries ({fraction:.0%}) have no "
                "expectation — the pass/fail side of the loss is mostly absent"
            ),
            detail={
                "entries_without_expectation": len(without),
                "total_entries": total,
                "fraction": fraction,
                "threshold": threshold,
                "entry_ids_without_expectation": sorted(entry.id for entry in without),
            },
        )
    ]


def detect_stalled_loop(
    experiments: list[Any], config: HealthConfig | None = None
) -> list[HealthFinding]:
    """Fire when N generations in a row were rejected by the tournament.

    Scans the most-recent evaluated experiments and counts the trailing
    run of ``rejected`` tournament decisions. When that run reaches
    ``config.stalled_rejects``, the proposer has not found an
    improvement in N consecutive rounds — the circuit breaker is about
    to (or already has) fired.

    Severity is ``warning``: a stall is not fatal — the next round may
    still win — but it is the operator's cue that the proposer is stuck
    and the rubric or mutable surface may need attention.

    Silent when fewer than ``config.stalled_rejects`` evaluated
    experiments exist or the trailing run is shorter than the threshold.

    ``config`` defaults to the env-sourced
    :class:`~zicato.config.HealthConfig` via :func:`load_config`.
    """
    threshold = _resolve_health_config(config).stalled_rejects

    decisions: list[tuple[str, str]] = []
    for exp in experiments:
        outcome = _attr_or_key(exp, "outcome")
        if outcome is None:
            continue
        decision = _attr_or_key(outcome, "tournament_decision")
        if decision is None:
            continue
        generation_id = str(_attr_or_key(exp, "generation_id") or "")
        decisions.append((generation_id, str(decision)))

    trailing_rejects: list[str] = []
    for generation_id, decision in reversed(decisions):
        if decision == "rejected":
            trailing_rejects.append(generation_id)
        else:
            break
    trailing_rejects.reverse()

    if len(trailing_rejects) < threshold:
        return []

    return [
        HealthFinding(
            code="stalled_loop",
            severity="warning",
            summary=(
                f"{len(trailing_rejects)} consecutive generations rejected — "
                "the proposer is not finding improvements"
            ),
            detail={
                "consecutive_rejects": len(trailing_rejects),
                "threshold": threshold,
                "rejected_generation_ids": trailing_rejects,
                "recommendation": (
                    "review the rubric and the mutable surface; the circuit "
                    "breaker is about to or has fired"
                ),
            },
        )
    ]


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _utcnow_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with ``Z`` suffix."""
    return _dt.datetime.now(_dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def assess_loop_health(
    losses_by_generation: dict[str, list[LossProfile]],
    experiments: list[Any],
    board_entries: list[BoardEntry],
    epoch_id: str,
    config: HealthConfig | None = None,
) -> LoopHealth:
    """Run every detector and collect the findings into a :class:`LoopHealth`.

    Parameters
    ----------
    losses_by_generation:
        ``{generation_id: [LossProfile, ...]}`` — the per-run reducer
        output for every generation under the epoch. Generation ids
        should be insertion-ordered oldest-first so window-based
        detectors see a stable view.
    experiments:
        Resolved :class:`~zicato.core.types.Experiment` records (or the
        plain dicts ``experiment.json`` deserialises to) in lineage
        order, oldest-first. Experiments without a tournament outcome
        are tolerated and skipped by the outcome-based detectors.
    board_entries:
        The epoch's frozen board.
    epoch_id:
        The epoch the report describes.
    config:
        The :class:`~zicato.config.HealthConfig` carrying the detector
        thresholds. Defaults to the env-sourced configuration via
        :func:`zicato.config.load_config`; resolved once here and passed
        to every threshold-using detector so the environment is read at
        most once per assessment.

    Returns
    -------
    LoopHealth
        ``healthy`` is ``True`` iff no finding has ``"warning"`` or
        ``"critical"`` severity.
    """
    health = _resolve_health_config(config)
    findings: list[HealthFinding] = []
    findings.extend(detect_degenerate_scoring(experiments, health))
    findings.extend(detect_non_differentiating_entry(losses_by_generation))
    findings.extend(detect_flat_drift_signal(losses_by_generation))
    findings.extend(detect_no_expectations(board_entries, health))
    findings.extend(detect_stalled_loop(experiments, health))

    healthy = not any(finding.severity in ("warning", "critical") for finding in findings)
    return LoopHealth(
        epoch_id=epoch_id,
        findings=tuple(findings),
        healthy=healthy,
        checked_at=_utcnow_iso(),
    )


__all__ = [
    "DEGENERATE_SCORING_WINDOW",
    "DEGENERATE_SCORING_EPSILON",
    "NO_EXPECTATIONS_FRACTION",
    "STALLED_LOOP_REJECTS",
    "HealthFinding",
    "LoopHealth",
    "assess_loop_health",
    "detect_degenerate_scoring",
    "detect_non_differentiating_entry",
    "detect_flat_drift_signal",
    "detect_no_expectations",
    "detect_stalled_loop",
]
