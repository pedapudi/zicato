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
meaning. An operator re-tunes between runs in the ``health`` block of
the workspace ``config.json`` (parsed by
:func:`zicato.config.health_config_from_workspace`; the former
``ZICATO_HEALTH_*`` env vars are deleted) or, when embedding zicato, by
constructing a :class:`~zicato.config.HealthConfig` directly. This
module never reads ``os.environ`` itself — every detector takes the
config as an optional parameter.

================================  =========
HealthConfig / ``health`` key      Default
================================  =========
``scoring_window``                       3
``scoring_epsilon``                   1e-6
``no_expectations_fraction``           0.5
``stalled_rejects``                      3
``generalization_gap_warn``           0.05
``generalization_gap_crit``           0.15
================================  =========

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
* ``generalization_gap_warn`` / ``generalization_gap_crit`` — the
  ``holdout_loss - train_loss`` gap at/above which
  :func:`detect_generalization_gap` fires ``warning`` / ``critical``
  (the "running-but-fake-progress" overfitting detector; OVERFITTING.md
  §6 / §12 #5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from zicato.config import HealthConfig, load_config
from zicato.core.experiment import PLACEBO_HYPOTHESIS_MARKER
from zicato.core.runtime import PREFLIGHT_GATE_DEFAULT
from zicato.core.types import BoardEntry, LossProfile
from zicato.util.iso_time import now_iso as _utcnow_iso

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

#: Generalization gap (``holdout_loss - train_loss``) at/above which
#: :func:`detect_generalization_gap` fires ``warning``. See
#: :attr:`zicato.config.HealthConfig.generalization_gap_warn`.
GENERALIZATION_GAP_WARN: float = HealthConfig().generalization_gap_warn

#: Generalization gap at/above which :func:`detect_generalization_gap`
#: fires ``critical`` (and recommends a board refresh). See
#: :attr:`zicato.config.HealthConfig.generalization_gap_crit`.
GENERALIZATION_GAP_CRIT: float = HealthConfig().generalization_gap_crit

#: Namespace prefix of drift-derived metrics in the unified metric view.
_DRIFT_NAMESPACE = "drift:"


def _resolve_health_config(config: HealthConfig | None) -> HealthConfig:
    """Return ``config`` if given, else the typed-config default.

    Detectors accept an optional :class:`~zicato.config.HealthConfig` so
    a caller that already holds one threads it in — the orchestrator and
    the ``zicato health`` command both build it from the workspace
    ``config.json``'s ``health`` block via
    :func:`zicato.config.health_config_from_workspace`. When a caller
    passes nothing, :func:`zicato.config.load_config` supplies the
    defaulted tree (plus any process-pinned overrides).
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
        ``"no_expectations"``, ``"stalled_loop"``,
        ``"generalization_gap"``, ``"refresh_cadence"``,
        ``"margin_below_noise_floor"``,
        ``"preflight_signal_below_floor"``,
        ``"preflight_saturated_contract"``, ``"preflight_inert_probe"``,
        ``"preflight_margin_above_achievable"``,
        ``"preflight_margin_below_floor"``, ``"noisy_judge"``,
        ``"dead_judge"``, ``"placebo_promoted"``, ``"infra_outage"``,
        ``"round_token_clipped"``, ``"tree_never_imported"``,
        ``"attributable_entry_regression"``,
        ``"on_promote_hook_failed"``.
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
        ``False`` — they are observations rather than problems.
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


def _float_or_none(value: Any) -> float | None:
    """Coerce a recorded number to ``float``, or ``None`` when it is not one.

    The same tolerance every detector applies to journal-sourced values:
    a missing key, a ``null``, or a malformed record must leave the
    quantity out of the finding rather than raise inside an assessment
    that is always best-effort.
    """
    if value is None:
        return None
    try:
        return float(value)
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


#: Longest bucketing key :func:`_reject_cause` will return before clipping.
_REJECT_CAUSE_CLIP = 60

#: Where a rejection reason stops naming its CAUSE and starts reporting the
#: numbers behind it. ``": "`` opens the detail clause on most reasons;
#: ``" ("`` opens the measured parenthetical on the rest; ``";"`` opens the
#: trailing "a promotion needs …" advice. The earliest one wins.
_REJECT_CAUSE_SEPARATORS = (": ", " (", ";")


def _reject_cause(reason: str) -> str:
    """Bucket a gate rejection reason down to its cause clause.

    Every reason :mod:`zicato.tournament.gate` composes leads with the
    rule that fired and follows it with that rule's measured numbers —
    ``"insufficient improvement: loss fell by only 0.0012 (champion … ->
    challenger …)"``. The numbers make each reason unique, which is
    exactly what a breakdown must NOT be keyed on.

    So the key is the text ahead of the earliest separator that opens a
    detail clause (:data:`_REJECT_CAUSE_SEPARATORS`) rather than the first colon
    alone. Some rules carry no colon at all and open straight into the
    parenthetical — ``"monotonicity_regression on namespace=rubric
    (champion 0.412345 -> …)"`` — and keying those on a length clip put
    the champion's aggregate INSIDE the key, so six rounds rejected by
    the same rule on the same namespace produced six singleton buckets
    and a breakdown longer than the summary it qualifies. Cutting at
    ``" ("`` keys them on the rule and the namespace, which is what the
    reader is counting.

    The clip stays as the last resort for a reason that carries no
    separator anywhere.
    """
    text = " ".join(str(reason or "").split())
    if not text:
        return "(no reason recorded)"
    cut = min(
        (i for i in (text.find(sep) for sep in _REJECT_CAUSE_SEPARATORS) if i > 0),
        default=-1,
    )
    head = (text[:cut] if cut > 0 else text).strip()
    if head and len(head) <= _REJECT_CAUSE_CLIP:
        return head
    return text[: _REJECT_CAUSE_CLIP - 1].rstrip() + "…"


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


def detect_dead_judge(
    losses_by_generation: dict[str, list[LossProfile]],
    board_entries: list[BoardEntry],
) -> list[HealthFinding]:
    """Split silent judges into BROKEN ones and genuinely dead ones.

    Every board entry's :attr:`BoardEntry.judges` declares one or more
    PROCESS judges. On a violation a judge emits a goldfive ``custom``
    drift the reducer attributes back to the judge as a
    ``custom:<judge_name>`` :class:`DriftCount` on the run's
    ``loss.json``. A judge whose attributed kind never appears in ANY run
    of the epoch fired zero times. That silence has two causes an operator
    must not confuse, and this detector emits a different finding for each:

    ``judge_erroring`` (the judge RAISED — read
        :attr:`~zicato.core.loss.LossProfile.judge_errors`)
        Its callable threw on some or all invocations: a misconfigured
        judge model, a revoked key, an endpoint outage. Both judge kinds
        swallow the exception by hard contract (a judge must not crash a
        run) and goldfive emits no event for the resulting empty verdict,
        so before per-judge error provenance existed this case was
        INDISTINGUISHABLE from the one below — and, being zero drift, it
        made the generation's scalar better than the truth. The fix is at
        the endpoint/model config rather than on the board.

    ``dead_judge`` (the judge ANSWERED, and always answered "no violation")
        Zero fires with zero errors: the judge is mis-wired (the events it
        keys on are never emitted) or its criterion is unreachable. It
        contributes no discrimination and gives a false sense of coverage.
        This is failure mode #3 in the board-audit playbook
        (``skills/zicato-audit-board``) and the "judge that never fires"
        smell in ``skills/zicato-design-judges``. The finding now says
        explicitly that errors were ruled out, because that is what makes
        a board audit the right next step rather than a wild goose chase.

    Both are ``warning``: a 0-fire judge is dead weight and a broken judge
    is a hole in coverage, but a board can still optimize on its other
    signals. Note the inverse is NOT a finding — a judge firing on EVERY
    run is loud rather than dead, and may be perfectly correct.

    Registered, NOT done here
    -------------------------
    * **No abort / tolerance knob.** A round in which one judge errored on
      100% of invocations is, from the artifacts alone, indistinguishable
      from a transient endpoint outage, and an outage never disqualifies a
      contract. Escalating "judge_erroring" into something that can stop or
      re-run a round is registered pending live evidence of what the error
      rates actually look like; until then this is observability only —
      nothing here changes a verdict, a score, or an exit code.
    * **No zicato-side timeout wrapper.** goldfive's
      ``DefaultSteerer.evaluate_judges`` bounds each ``evaluate`` with its
      own 30s timeout and treats an overrun as "no signal" WITHOUT calling
      back into zicato, so a judge that hangs (rather than raises) still
      lands in the silent bucket and reads as ``dead_judge``. Closing that
      gap means either a zicato-side timeout inside ``evaluate`` (a second
      competing budget) or a goldfive change; both are registered, neither
      is in this pass.

    Silent when there are no runs yet (nothing has had a chance to fire)
    or no entry declares a judge (drift-/expectation-only board).
    """
    # Lazy import keeps the diagnostics module dependency-light and
    # `zicato --help` fast; the reducer owns the attributed-kind parse.
    from zicato.telemetry.reducer import split_judge_attributed_kind  # noqa: PLC0415

    declared: set[str] = set()
    for entry in board_entries:
        for judge in entry.judges:
            if judge.name:
                declared.add(judge.name)
    if not declared:
        return []

    total_runs = 0
    fired: set[str] = set()
    # judge_name -> [invocations, errors, last_error_type], summed over every
    # run of the epoch. ``getattr`` keeps the detector working against a
    # loss-like record written before the field existed.
    errored: dict[str, list[Any]] = {}
    for losses in losses_by_generation.values():
        for loss in losses:
            total_runs += 1
            for count in loss.drift_counts:
                is_custom, judge_name = split_judge_attributed_kind(count.kind)
                if is_custom and judge_name:
                    fired.add(judge_name)
            for je in getattr(loss, "judge_errors", ()) or ():
                name = str(getattr(je, "judge_name", "") or "")
                if not name or not int(getattr(je, "errors", 0) or 0):
                    continue
                row = errored.setdefault(name, [0, 0, ""])
                row[0] = int(row[0]) + int(getattr(je, "invocations", 0) or 0)
                row[1] = int(row[1]) + int(getattr(je, "errors", 0) or 0)
                row[2] = str(getattr(je, "last_error_type", "") or "") or row[2]

    # No runs yet → no judge has had a chance to fire; stay silent rather
    # than flag every declared judge on an epoch with no telemetry.
    if total_runs == 0:
        return []

    findings: list[HealthFinding] = []

    # A judge that raised is BROKEN rather than dead weight — a distinct finding
    # with a distinct remedy (the endpoint/model config rather than the board).
    broken = sorted(name for name in errored if name in declared)
    if broken:
        counts = {
            name: {
                "invocations": errored[name][0],
                "errors": errored[name][1],
                "last_error_type": errored[name][2],
            }
            for name in broken
        }
        phrases = ", ".join(
            f"{name!r} raised on {counts[name]['errors']}/{counts[name]['invocations']} "
            f"invocations ({counts[name]['last_error_type'] or 'unknown error'})"
            for name in broken
        )
        findings.append(
            HealthFinding(
                code="judge_erroring",
                severity="warning",
                summary=(
                    f"{len(broken)} board-declared judge(s) FAILED to answer across "
                    f"the epoch's {total_runs} runs — their zero drift is an ERROR "
                    f"artifact rather than a verdict: {phrases}"
                ),
                detail={
                    "erroring_judges": broken,
                    "judge_error_counts": counts,
                    "declared_judges": sorted(declared),
                    "runs_inspected": total_runs,
                    "recommendation": (
                        "a judge that raised did not decide anything — its silence "
                        "lowered the generation's drift loss without evidence. Check "
                        "the auxiliary/judge endpoint and model config (the judge role "
                        "in the workspace models config) before reading this epoch's "
                        "scores; a board audit is the WRONG next step here"
                    ),
                },
            )
        )

    # Genuinely dead: zero fires AND zero errors — the judge answered every
    # time, and every answer was "no violation".
    dead = sorted(declared - fired - set(errored))
    if dead:
        findings.append(
            HealthFinding(
                code="dead_judge",
                severity="warning",
                summary=(
                    f"{len(dead)} board-declared judge(s) never fired across all "
                    f"{total_runs} runs in the epoch and contribute no coverage: "
                    + ", ".join(repr(name) for name in dead)
                ),
                detail={
                    "dead_judges": dead,
                    "declared_judges": sorted(declared),
                    "fired_judges": sorted(fired),
                    "runs_inspected": total_runs,
                    "recommendation": (
                        "these judges recorded NO call failures, so the silence is a "
                        "real verdict rather than a broken endpoint: confirm each judge "
                        "is wired to events that actually fire and its criterion is "
                        "reachable; if it can never fire, remove it or sharpen its "
                        "body (see zicato-design-judges)"
                    ),
                },
            )
        )

    return findings


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

    The finding names WHY each round lost, bucketed by gate reason (issue
    #129). "Six rejections" is one number covering several very different
    situations: six near-misses on the promotion margin say the proposer
    is close and the margin may be mis-set, whereas six pass-rate
    regressions on the same entry say the mutable surface keeps breaking
    one thing. Those demand different responses, and the reasons are
    already on the outcomes the detector walks.

    ``config`` defaults to the env-sourced
    :class:`~zicato.config.HealthConfig` via :func:`load_config`.
    """
    threshold = _resolve_health_config(config).stalled_rejects

    decisions: list[tuple[str, str, str]] = []
    for exp in experiments:
        outcome = _attr_or_key(exp, "outcome")
        if outcome is None:
            continue
        decision = _attr_or_key(outcome, "tournament_decision")
        if decision is None:
            continue
        generation_id = str(_attr_or_key(exp, "generation_id") or "")
        reason = str(_attr_or_key(outcome, "rejection_reason") or "")
        decisions.append((generation_id, str(decision), reason))

    trailing_rejects: list[str] = []
    trailing_reasons: list[str] = []
    for generation_id, decision, reason in reversed(decisions):
        if decision == "rejected":
            trailing_rejects.append(generation_id)
            trailing_reasons.append(reason)
        else:
            break
    trailing_rejects.reverse()
    trailing_reasons.reverse()

    if len(trailing_rejects) < threshold:
        return []

    causes: dict[str, int] = {}
    for reason in trailing_reasons:
        cause = _reject_cause(reason)
        causes[cause] = causes.get(cause, 0) + 1
    # Commonest cause first; ties keep first-seen (lineage) order.
    ranked = sorted(causes.items(), key=lambda kv: -kv[1])
    breakdown = ", ".join(f"{count}x {cause}" for cause, count in ranked)

    return [
        HealthFinding(
            code="stalled_loop",
            severity="warning",
            summary=(
                f"{len(trailing_rejects)} consecutive generations rejected — "
                f"the proposer is not finding improvements ({breakdown})"
            ),
            detail={
                "consecutive_rejects": len(trailing_rejects),
                "threshold": threshold,
                "rejected_generation_ids": trailing_rejects,
                "rejection_causes": dict(ranked),
                "rejection_reasons": dict(zip(trailing_rejects, trailing_reasons, strict=True)),
                "recommendation": (
                    "review the rubric and the mutable surface; the circuit "
                    "breaker is about to or has fired"
                ),
            },
        )
    ]


def _gap_observation(experiment: Any) -> tuple[float, float, float] | None:
    """Return ``(train_loss, holdout_loss, generalization_gap)`` for an exp.

    Reads the per-generation loss fields off the experiment's tournament
    ``outcome`` (OVERFITTING.md §12 #5), tolerating both a typed
    :class:`~zicato.core.types.OutcomeRecord` and the plain dict
    ``experiment.json`` deserialises to. Returns ``None`` whenever there is
    no measured holdout — no outcome, no holdout loss, or a missing gap —
    which is the safe degrade: such generations contribute no signal.
    """
    outcome = _attr_or_key(experiment, "outcome")
    if outcome is None:
        return None
    train = _attr_or_key(outcome, "train_loss")
    holdout = _attr_or_key(outcome, "holdout_loss")
    gap = _attr_or_key(outcome, "generalization_gap")
    if train is None or holdout is None or gap is None:
        return None
    try:
        return float(train), float(holdout), float(gap)
    except (TypeError, ValueError):
        return None


def detect_generalization_gap(
    experiments: list[Any], config: HealthConfig | None = None
) -> list[HealthFinding]:
    """Fire when the champion's holdout loss diverges from its train loss.

    The "running-but-fake-progress" detector (OVERFITTING.md §6 / §12 #5),
    the overfitting counterpart to the "running-but-meaningless" family. It
    watches the **generalization gap** — ``holdout_loss - train_loss`` — over
    the lineage. When the proposer begins *memorizing the board* rather than
    improving true quality, the train loss keeps falling while the holdout
    loss stalls or rises, so the gap **widens** and turns positive (the
    holdout scores worse than the train slice).

    The rule, over the generations that carry a measured holdout (both
    ``train_loss`` and ``holdout_loss`` persisted on the outcome):

    * Fewer than two such generations → no finding (nothing to compare; the
      safe degrade when there is no holdout or the run is too young).
    * Otherwise compute the latest gap and whether it **widened** since the
      earliest measured generation (``gap_now > gap_first``). A gap that is
      flat or *narrowing* is healthy regardless of magnitude — the holdout is
      tracking train — so the detector clears.
    * A widened gap at/above ``config.generalization_gap_crit`` fires
      ``critical`` and recommends a board refresh (OVERFITTING.md §7 / §12 #6:
      roll the epoch / rotate the holdout); at/above
      ``config.generalization_gap_warn`` it fires ``warning``; below the warn
      bar it clears.

    ``config`` defaults to the env-sourced
    :class:`~zicato.config.HealthConfig` via :func:`load_config`.
    """
    health = _resolve_health_config(config)
    warn = health.generalization_gap_warn
    crit = max(health.generalization_gap_crit, warn)

    observations: list[tuple[str, float, float, float]] = []
    for exp in experiments:
        obs = _gap_observation(exp)
        if obs is None:
            continue
        generation_id = str(_attr_or_key(exp, "generation_id") or "")
        observations.append((generation_id, *obs))

    if len(observations) < 2:
        return []

    first_gap = observations[0][3]
    gen_now, train_now, holdout_now, gap_now = observations[-1]

    # A flat or narrowing gap is healthy — the holdout tracks train.
    if gap_now <= first_gap:
        return []
    # Below the warning bar there is no concern yet.
    if gap_now < warn:
        return []

    if gap_now >= crit:
        severity = "critical"
        recommendation = (
            "the champion is memorizing the board — refresh the contract: roll "
            "the epoch (rotating the holdout) or author fresh entries "
            "(OVERFITTING.md §7; SELECTION-THEORY.md §5 optimal-stopping horizon)"
        )
    else:
        severity = "warning"
        recommendation = (
            "watch the holdout vs train divergence; if it keeps widening, "
            "refresh the contract / roll the epoch (OVERFITTING.md §6–§7)"
        )

    return [
        HealthFinding(
            code="generalization_gap",
            severity=severity,
            summary=(
                f"generalization gap widened to {gap_now:+.3f} "
                f"(holdout_loss={holdout_now:g} − train_loss={train_now:g}) at "
                f"generation {gen_now!r} — the champion may be overfitting the board"
            ),
            detail={
                "generation_id": gen_now,
                "train_loss": train_now,
                "holdout_loss": holdout_now,
                "generalization_gap": gap_now,
                "first_generalization_gap": first_gap,
                "warn_threshold": warn,
                "crit_threshold": crit,
                "generations_with_holdout": len(observations),
                "refresh_recommended": severity == "critical",
                "recommendation": recommendation,
            },
        )
    ]


def detect_refresh_cadence(
    experiments: list[Any], max_generations_per_contract: int | None
) -> list[HealthFinding]:
    """Recommend a board refresh once a contract has been mined long enough.

    The cadence half of OVERFITTING.md §7 / §12 #6. When the operator sets
    :attr:`~zicato.core.types.OverfittingConfig.max_generations_per_contract`,
    this surfaces a board-refresh **recommendation** (an ``info`` finding,
    never a forced auto-roll) once the number of evaluated generations under
    the contract reaches that ceiling — a cue that the contract has been
    mined enough and should be refreshed (cross-ref SELECTION-THEORY.md §5,
    the optimal-stopping horizon). The companion overfitting signal is the
    ``critical`` :func:`detect_generalization_gap` finding; either is the
    operator's cue to roll.

    Silent when no ceiling is configured (``None`` — the default) or the
    contract has not yet reached it.
    """
    if max_generations_per_contract is None:
        return []
    evaluated = sum(1 for exp in experiments if _attr_or_key(exp, "outcome") is not None)
    if evaluated < max_generations_per_contract:
        return []
    return [
        HealthFinding(
            code="refresh_cadence",
            severity="info",
            summary=(
                f"{evaluated} generations evaluated under this contract — at/over the "
                f"configured cadence ceiling of {max_generations_per_contract}; consider "
                "refreshing the board (roll the epoch)"
            ),
            detail={
                "evaluated_generations": evaluated,
                "max_generations_per_contract": max_generations_per_contract,
                "refresh_recommended": True,
                "recommendation": (
                    "refresh the contract — roll the epoch (rotating the holdout) "
                    "(OVERFITTING.md §7; SELECTION-THEORY.md §5 optimal-stopping horizon)"
                ),
            },
        )
    ]


def detect_margin_below_noise_floor(
    noise_floor: dict[str, Any] | None,
    promote_margin: float | None,
    evidence_gate_on: bool,
) -> list[HealthFinding]:
    """Warn when ``promote_margin`` sits inside the measured A/A noise.

    The calibration half of the noise-aware decision procedure: when an
    A/A audit (:mod:`zicato.tournament.calibration`) has measured the
    epoch's noise floor and the contract's ``promote_margin`` is strictly
    below it, a duel decided by the margin alone cannot distinguish a real
    improvement from a re-roll of the same generation. With the evidence
    gate ON the defer→replicate loop absorbs that noise, so the finding is
    downgraded to an ``info`` observation; with the gate explicitly OFF it
    is a ``warning`` — the loop is promoting/rejecting on noise.

    Silent when no floor was ever measured (``noise_floor is None``), when
    the floor record is malformed, or when the margin clears the floor.
    """
    if promote_margin is None:
        return []
    from zicato.tournament.calibration import margin_below_floor  # noqa: PLC0415

    if not margin_below_floor(promote_margin, noise_floor):
        return []
    assert isinstance(noise_floor, dict)  # narrowed by margin_below_floor
    max_abs = float(noise_floor.get("max_abs_delta", 0.0))
    severity = "info" if evidence_gate_on else "warning"
    gate_note = (
        "the evidence gate is ON, so the defer→replicate loop still holds "
        "promotions to CI separation"
        if evidence_gate_on
        else "the evidence gate is OFF — duels are decided by the margin alone"
    )
    return [
        HealthFinding(
            code="margin_below_noise_floor",
            severity=severity,
            summary=(
                f"promote_margin {promote_margin:.6g} is below the measured A/A "
                f"noise floor {max_abs:.6g}; {gate_note}"
            ),
            detail={
                "promote_margin": promote_margin,
                "noise_floor_max_abs_delta": max_abs,
                "noise_floor_runs": noise_floor.get("runs"),
                "noise_floor_generation_id": noise_floor.get("generation_id"),
                "evidence_gate_on": evidence_gate_on,
                "recommendation": (
                    "raise promote_margin above the measured floor, or keep "
                    "promote_confidence_threshold set so promotions replicate "
                    "to CI separation"
                ),
            },
        )
    ]


#: Pairwise test–retest disagreement rate above which a judge counts as
#: noisy. Mirrors
#: :data:`zicato.judge_runtime.reliability.NOISY_JUDGE_DISAGREEMENT_THRESHOLD`
#: (kept as a plain value here so this module stays dependency-light).
NOISY_JUDGE_DISAGREEMENT: float = 0.25


def detect_noisy_judge(
    reliabilities: list[Any],
    threshold: float = NOISY_JUDGE_DISAGREEMENT,
) -> list[HealthFinding]:
    """Flag judges whose test–retest disagreement exceeds ``threshold``.

    Input is the output of a judge test–retest probe
    (:func:`zicato.judge_runtime.reliability.test_retest_board`) — one
    record per judge, as :class:`JudgeReliability` objects or their
    ``to_json`` dicts. A judge that returns different verdicts for a
    byte-identical frozen transcript injects pure noise into every
    ``custom:<judge_name>`` drift count it produces; the finding is a
    ``warning`` (recommend-only) whose recommendation points at the
    contract's routing knob for exactly this signal:
    ``per_judge_weights`` (down-weight the judge) — or sharpening the
    criterion until the retest stabilises.

    One finding per noisy judge; silent for an empty probe or when every
    judge's disagreement is at or below the threshold.
    """
    findings: list[HealthFinding] = []
    for rel in reliabilities:
        try:
            rate = float(_attr_or_key(rel, "disagreement_rate") or 0.0)
        except (TypeError, ValueError):
            continue
        if rate <= threshold:
            continue
        name = str(_attr_or_key(rel, "judge_name") or "")
        k = _attr_or_key(rel, "k")
        fired = _attr_or_key(rel, "fired")
        findings.append(
            HealthFinding(
                code="noisy_judge",
                severity="warning",
                summary=(
                    f"judge {name!r} disagreed with itself on {rate:.0%} of verdict "
                    f"pairs over the SAME frozen transcript (fired {fired}/{k}) — "
                    "its drift signal is noise rather than judgement"
                ),
                detail={
                    "judge_name": name,
                    "k": k,
                    "fired": fired,
                    "disagreement_rate": rate,
                    "threshold": threshold,
                    "recommendation": (
                        f"down-weight it (scoring per_judge_weights[{name!r}] below "
                        "the default) or sharpen its criterion until test-retest "
                        "stabilises (see zicato board judges --test-retest)"
                    ),
                },
            )
        )
    return findings


def _is_placebo_experiment(experiment: Any) -> bool:
    """Whether an experiment record is the random-baseline placebo arm.

    Keyed on the stable
    :data:`zicato.core.experiment.PLACEBO_HYPOTHESIS_MARKER` prefix of
    ``hypothesis.core_idea`` (the contract with the minting side,
    :mod:`zicato.evolve.placebo`). Tolerant of typed records and plain
    ``experiment.json`` dicts via the usual :func:`_attr_or_key` shim.
    """
    hypothesis = _attr_or_key(experiment, "hypothesis")
    if hypothesis is None:
        return False
    core_idea = _attr_or_key(hypothesis, "core_idea")
    return isinstance(core_idea, str) and core_idea.startswith(PLACEBO_HYPOTHESIS_MARKER)


def detect_placebo_promoted(
    experiments: list[Any],
    promote_margin: float | None = None,
    noise_floor: dict[str, Any] | None = None,
) -> list[HealthFinding]:
    """CRITICAL when a random-baseline (placebo) challenger was PROMOTED.

    The placebo arm (OVERFITTING.md #7,
    ``overfitting.random_baseline_every_n``) fields a semantics-preserving
    no-op challenger the gate MUST reject — identical behaviour leaves no
    improvement to clear the margin. A promoted placebo therefore means
    the decision procedure is promoting noise: gate discrimination is
    broken (margin inside the noise floor, a broken reducer, a rigged
    gate) and every recent real "win" is suspect. One ``critical``
    finding per promoted placebo generation.

    Silent when no placebo experiments exist (the knob is off / no
    cadence tick yet) or every placebo was rejected — the arm doing its
    quiet calibration job.

    ``promote_margin`` and ``noise_floor`` are the epoch's decision
    parameters, threaded from :func:`assess_loop_health` so the alarm can
    show the comparison that failed rather than only its verdict (issue
    #129). The two readings call for different operator moves. A no-op
    whose measured delta cleared a margin that sits inside the measured
    noise is a mis-set margin. The same delta clearing a margin well above
    the floor is a broken reducer or a rigged gate. Both default to ``None`` (never
    measured / not supplied), which simply leaves that clause off the
    line. Absolute parent and child scalars are NOT available here — an
    :class:`~zicato.core.experiment.Outcome` records deltas only.
    """
    floor = (
        _float_or_none(noise_floor.get("max_abs_delta")) if isinstance(noise_floor, dict) else None
    )

    findings: list[HealthFinding] = []
    for exp in experiments:
        if not _is_placebo_experiment(exp):
            continue
        outcome = _attr_or_key(exp, "outcome")
        if outcome is None:
            continue
        decision = str(_attr_or_key(outcome, "tournament_decision") or "")
        if decision != "promoted":
            continue
        generation_id = str(_attr_or_key(exp, "generation_id") or "")
        delta = _float_or_none(_attr_or_key(outcome, "scalar_score_delta"))
        measured = f"scalar delta {delta:.6g}" if delta is not None else "an unrecorded delta"
        compared = ""
        if promote_margin is not None:
            compared = f" against promote_margin {promote_margin:.6g}"
            if floor is not None:
                compared += f" (measured noise floor {floor:.6g})"
        elif floor is not None:
            compared = f" (measured noise floor {floor:.6g})"
        findings.append(
            HealthFinding(
                code="placebo_promoted",
                severity="critical",
                summary=(
                    f"random-baseline placebo {generation_id!r} was PROMOTED on "
                    f"{measured}{compared} — a semantics-preserving no-op won a "
                    "tournament, so the gate is promoting noise; recent "
                    "promotions are suspect"
                ),
                detail={
                    "generation_id": generation_id,
                    "scalar_score_delta": _attr_or_key(outcome, "scalar_score_delta"),
                    "pass_rate_delta": _attr_or_key(outcome, "pass_rate_delta"),
                    "drift_loss_delta": _attr_or_key(outcome, "drift_loss_delta"),
                    "promote_margin": promote_margin,
                    "noise_floor_max_abs_delta": floor,
                    "recommendation": (
                        "stop and audit the decision procedure: re-measure the "
                        "noise floor (zicato board audit / board preflight), "
                        "raise promote_margin above it, keep the evidence gate "
                        "on, and re-examine recent promotions"
                    ),
                },
            )
        )
    return findings


def detect_preflight_verdict(
    preflight: dict[str, Any] | None,
    preflight_gate: str = PREFLIGHT_GATE_DEFAULT,
) -> list[HealthFinding]:
    """Re-surface a non-OK contract pre-flight verdict as a health finding.

    The contract pre-flight (:mod:`zicato.epoch.preflight`) measures the
    epoch's A/A noise floor AND its degradation signal (champion vs a
    degraded copy of itself) before rounds burn budget. Its
    verdict persists onto the epoch record; this detector folds it into
    every round's health report so the operator keeps seeing it for as
    long as the contract stays un-fixed. Recommend-only — like every
    finding, it never gates.

    ``preflight_gate`` is the operator's
    :attr:`~zicato.core.runtime.RuntimeConfig.preflight_gate` mode, and it
    decides the SEVERITY of the refusal finding — see the severity contract
    below.

    * verdict ``"refuse"`` → ``preflight_signal_below_floor``: the measured
      signal is at or below the measured noise floor, so duels
      under this contract are decided by noise. ``critical`` only under
      ``preflight_gate="refuse"``; ``warning`` under ``"warn"`` (the default)
      and ``"off"``.
    * verdict ``"warn"`` → ``warning`` ``preflight_saturated_contract``:
      every probe — K A/A draws plus a degraded tree —
      scored identically (the historical ``1.000000`` signature); the
      contract cannot discriminate candidates.
    * verdict ``"inert"`` → ``warning`` ``preflight_inert_probe`` (issue
      #106): every point the pre-flight degraded moved the scalar by
      exactly nothing while the champion's own draws did vary. The
      signal is UNMEASURED rather than zero, so the finding must not
      read like the noise-limited one — the fix is to pin a representative
      point, and the protection simply is not in force meanwhile.

    Independently of the signal verdict, a ``promote_margin`` window failure
    (issue #112, ``window_failure``) yields ONE further finding — the two
    questions are separable and a contract can fail either alone:

    * ``"margin_above_achievable"`` → ``warning``
      ``preflight_margin_above_achievable``: the margin exceeds the measured
      DEGRADATION signal. Not critical, and after issue #119 not even strong
      evidence: what the probe measures is how far the scalar fell when a
      mutation point was destroyed, which does not bound how far a challenger
      can improve (and, degrading one point per probe, under-reports even
      that). The finding names a number worth checking rather than a null run.
    * ``"margin_below_floor"`` → ``warning`` ``preflight_margin_below_floor``.
    * ``"empty_window"`` is NOT a finding of its own — it is the same fact the
      refuse/inert finding already carries — but it rewrites that finding's
      recommendation to say no margin is defensible, so an operator is not
      sent to tune a number that has no valid value.

    Severity respects the gate mode
    -------------------------------

    The refusal finding is a ``critical`` ONLY when the operator asked for the
    hard gate. Under the default ``preflight_gate="warn"`` it is a
    ``warning``: fully visible in ``zicato health``, in the per-round report
    and on the dashboard, but structurally unable to reach
    :class:`zicato.evolve.loop.DegenerateHealthPolicy`, which observes
    criticals only.

    That asymmetry is the point. This detector re-emits from the PERSISTED
    record, so a refuse verdict reappears identically every round for as long
    as the epoch carries it — which means a ``critical`` here is not one
    finding, it is an unbroken critical streak, and two rounds of it trip
    ``_DEGENERATE_HEALTH_STOP_THRESHOLD`` and stop the loop. A run the
    operator explicitly configured to WARN would then hard-stop anyway, making
    the knob's ``"warn"`` setting indistinguishable from ``"refuse"`` except
    for wasting two rounds first. A genuinely noise-limited contract under
    ``"warn"`` must keep running with a loud warning — that is what "warn"
    means. Under ``"refuse"`` the loop already stopped at the pre-flight
    itself, so the breaker is moot and ``critical`` costs nothing.

    Silent when no pre-flight was ever run (``None``), when the record is
    malformed, or when the verdict is ``"ok"`` with the window intact.
    Tolerant of a record that carries neither key.
    """
    if not isinstance(preflight, dict):
        return []
    hard_gate = str(preflight_gate or PREFLIGHT_GATE_DEFAULT) == "refuse"
    verdict = str(preflight.get("verdict", "") or "")
    window_failure = str(preflight.get("window_failure") or "")
    if verdict not in ("refuse", "warn", "inert") and not window_failure:
        return []

    def _num(key: str) -> float:
        try:
            return float(preflight.get(key, 0.0))
        except (TypeError, ValueError):
            return 0.0

    signal = _num("signal")
    floor = _num("noise_floor_max_abs_delta")
    margin = _num("promote_margin")
    detail: dict[str, Any] = {
        "verdict": verdict,
        "signal": signal,
        "noise_floor_max_abs_delta": floor,
        "champion_scalars": preflight.get("champion_scalars"),
        "degraded_scalar": preflight.get("degraded_scalar"),
        "degraded_mutation_id": preflight.get("degraded_mutation_id"),
        "generation_id": preflight.get("generation_id"),
        "measured_at": preflight.get("measured_at"),
        "probed_points": preflight.get("probed_points"),
        "promote_margin": margin,
        "window_verdict": preflight.get("window_verdict"),
        "window_failure": preflight.get("window_failure"),
        # The gate mode the severity was chosen under, so a reader of a
        # persisted report can tell "warning because the operator chose warn"
        # from "warning because the detector graded it mild".
        "preflight_gate": str(preflight_gate or PREFLIGHT_GATE_DEFAULT),
    }
    empty_window = window_failure == "empty_window"
    findings: list[HealthFinding] = []

    if verdict == "refuse":
        detail = {
            **detail,
            "recommendation": (
                "no promote_margin is defensible on this board — do not tune the "
                "margin; reduce evaluation noise (more replicates, steadier "
                "judges) or strengthen the board so a real change out-scores a "
                "re-roll"
                if empty_window
                else "refusal recommended: the contract's measured signal does "
                "not clear its own noise floor — reduce evaluation noise (more "
                "replicates, steadier judges) or strengthen the board before "
                "running rounds"
            ),
        }
        findings.append(
            HealthFinding(
                code="preflight_signal_below_floor",
                # Gate-aware on purpose (see the docstring): a critical here
                # re-fires every round from the persisted record, and two of
                # them trip the loop's degenerate-health breaker — which would
                # turn the DEFAULT "warn" mode into a hard stop and override
                # the operator's explicit choice.
                severity="critical" if hard_gate else "warning",
                summary=(
                    f"contract pre-flight: measured signal {signal:.6g} is at/below "
                    f"the measured A/A noise floor {floor:.6g} — duels under this "
                    "contract are decided by noise (refusal recommended)"
                ),
                detail=detail,
            )
        )
    elif verdict == "inert":
        probed = preflight.get("probed_points")
        # Only points that actually SPENT a draw are evidence. ``probed_points``
        # also carries the ones dropped for free (``no_op_patch`` /
        # ``verdict_settled``), and counting those would tell an operator the
        # sample was broader than the measurement really was — the opposite of
        # what the field exists for.
        n_probed = (
            sum(1 for p in probed if isinstance(p, dict) and not p.get("skipped"))
            if isinstance(probed, list)
            else 0
        )
        findings.append(
            HealthFinding(
                code="preflight_inert_probe",
                severity="warning",
                summary=(
                    f"contract pre-flight: every probed mutation point "
                    f"({n_probed or 'all'}) left the scalar exactly at the champion "
                    f"mean while the A/A draws varied by {floor:.6g} — the achievable "
                    "signal is UNMEASURED because the probe was inert, which leaves "
                    "open whether the contract can discriminate at all"
                ),
                detail={
                    **detail,
                    "recommendation": (
                        "pin a mutation point the deliverable demonstrably depends on "
                        "(runtime.preflight_probe_mutation_ids, or `zicato board "
                        "preflight --degrade-mutation-id`) and re-measure; raising "
                        "runtime.preflight_probe_points widens the automatic sample. "
                        "A point can be inert because the contract routes around it — "
                        "e.g. a tool description no longer reached once a "
                        "structured-output schema produces the deliverable"
                    ),
                },
            )
        )
    elif verdict == "warn":
        findings.append(
            HealthFinding(
                code="preflight_saturated_contract",
                severity="warning",
                summary=(
                    "contract pre-flight: scalar spread was exactly zero across every "
                    "probe (K A/A draws + a deliberately-degraded tree) — the contract "
                    "cannot discriminate candidates (the 1.000000 saturation signature)"
                ),
                detail={
                    **detail,
                    "recommendation": (
                        "add expectations / strengthen judges so the board can "
                        "discriminate candidates — even a deliberately-degraded tree "
                        "scored identically to the champion"
                    ),
                },
            )
        )

    if window_failure == "margin_above_achievable":
        findings.append(
            HealthFinding(
                code="preflight_margin_above_achievable",
                # A WARNING, and after issue #119 that is not a judgement call
                # about strength of evidence — it is all the evidence there is.
                # The probe measures DEGRADATION headroom (how far the scalar
                # fell when a point was destroyed), which bounds a challenger's
                # improvement from neither side. On top of that it degrades ONE
                # point per probe, so it under-reports even the movement it does
                # measure (a compound patch — and recombination unions two on
                # purpose — exceeds it). Critical is reserved for the honest
                # measurement, "no usable signal at all", and it trips the loop's
                # degenerate-health circuit breaker.
                severity="warning",
                summary=(
                    f"contract pre-flight: promote_margin {margin:.6g} is at/above the "
                    f"measured degradation signal {signal:.6g} — the only movement the "
                    "probe demonstrated (destroying a mutation point) is smaller than "
                    "the margin. Improvement headroom is UNMEASURED: check the margin "
                    "rather than conclude that the run is null"
                ),
                detail={
                    **detail,
                    "recommendation": (
                        "check promote_margin against what a real fix on this board is "
                        f"worth; the measured degradation signal {signal:.6g} is a "
                        "single-point LOWER bound on movement and says nothing about "
                        "how much a challenger can improve — a champion sitting near "
                        "the failing end has little left to break and plenty to gain. "
                        f"The margin does need to clear the noise floor {floor:.6g}, "
                        "which is measured honestly. If the margin is deliberately "
                        "above single-point reach — e.g. recombination is expected to "
                        "union two sub-margin fixes — this finding is informational"
                    ),
                },
            )
        )
    elif window_failure == "margin_below_floor":
        findings.append(
            HealthFinding(
                code="preflight_margin_below_floor",
                severity="warning",
                summary=(
                    f"contract pre-flight: promote_margin {margin:.6g} is at/below the "
                    f"measured A/A noise floor {floor:.6g} — promotions cannot be "
                    "distinguished from re-rolls of the same generation"
                ),
                detail={
                    **detail,
                    "recommendation": (
                        "raise promote_margin above the measured noise (the pre-flight "
                        "record's recommended_margin scales the draw-count-stable "
                        "delta_std rather than the range), and/or keep the evidence "
                        "gate on so promotions must replicate to CI separation"
                    ),
                },
            )
        )
    return findings


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def detect_infra_outage(infra_outage: tuple[int, int] | None) -> list[HealthFinding]:
    """Surface a round deferred by the endpoint-outage circuit.

    ``infra_outage`` is the ``(infra_aborted_runs, threshold)`` pair the
    orchestrator observed when THIS round's infra-abort count crossed
    :attr:`~zicato.core.runtime.RuntimeConfig.infra_abort_round_threshold`
    and the round deferred instead of burning the experiment on
    worst-case-scored aborts. A ``warning`` — the loop is healthy but the
    ENDPOINT is not, and the operator should check it before the backoff
    schedule spends the remaining rounds. ``None`` (every round the
    circuit did not trip, including circuit-off) is silent — a runtime
    event, unlike every other detector's disk-derived inputs, so the
    orchestrator threads it in per round.
    """
    if infra_outage is None:
        return []
    aborted, threshold = infra_outage
    return [
        HealthFinding(
            code="infra_outage",
            severity="warning",
            summary=(
                f"round deferred: {aborted} infra-aborted run(s) reached the "
                f"endpoint-outage threshold of {threshold} — the model endpoint "
                "(or worker infrastructure) is failing; the experiment was kept "
                "un-outcomed and the loop is backing off"
            ),
            detail={
                "infra_aborted_runs": aborted,
                "infra_abort_round_threshold": threshold,
                "recommendation": (
                    "check the harness/auxiliary endpoint health and credentials; "
                    "the deferred round resumes (or discards cleanly) via the "
                    "standard crash-resume reconciliation"
                ),
            },
        )
    ]


def detect_token_budget_clip(token_clip: tuple[int, int] | None) -> list[HealthFinding]:
    """Surface a round the per-round token budget clipped.

    ``token_clip`` is the ``(tokens_spent, max_tokens_per_round)`` pair
    the orchestrator observed when the round's ledger latched its clip
    flag — a scheduler stopped launching board units / replicate slots on
    the spent budget and the round settled with what it had. A
    ``warning``: the round's verdict rests on partial coverage (un-run
    units scored as budget-exceeded losses), so the operator should size
    the budget against the board before trusting a streak of clipped
    rounds. ``None`` (every unclipped round, including budget-off) is
    silent — a runtime event threaded per round by the orchestrator, like
    :func:`detect_infra_outage`.
    """
    if token_clip is None:
        return []
    spent, budget = token_clip
    return [
        HealthFinding(
            code="round_token_clipped",
            severity="warning",
            summary=(
                f"round token-clipped: {spent} tokens spent reached the per-round "
                f"budget of {budget} — further board units/replicates were not "
                "scheduled and the round settled on partial coverage"
            ),
            detail={
                "tokens_spent": spent,
                "max_tokens_per_round": budget,
                "recommendation": (
                    "raise runtime.max_tokens_per_round (or shrink the board / "
                    "replicates) so a full round fits the budget; a clipped round "
                    "scores its un-run units as budget-exceeded losses"
                ),
            },
        )
    ]


def detect_tree_never_imported(
    tree_import_gaps: dict[str, tuple[str, ...]] | None,
) -> list[HealthFinding]:
    """Surface a generation whose units NEVER imported a mutable tree.

    ``tree_import_gaps`` is ``{generation_id: (tree_basename, ...)}`` — the
    trees no unit of that generation ever imported, accumulated by the
    tournament workers into each generation's ``harness_load.json`` and read
    back by :func:`zicato.health.inputs.epoch_tree_import_gaps`.

    This is the ONLY detector of a fully shadowed snapshot: an entrypoint that
    resolves to an INSTALLED copy under a different top-level name and never
    imports the mutated tree at all (issue #110). Nothing else notices — the run completes,
    the board scores, the gate fires, the round promotes or rejects on a
    comparison between two identical unmutated trees. A ``warning`` rather than
    ``critical`` because a single generation can have a benign cause (a board
    whose entries genuinely exercise only part of the surface), and the
    operator rather than the detector, owns that judgement.

    ``None`` / empty (every healthy round) is silent.
    """
    if not tree_import_gaps:
        return []
    findings: list[HealthFinding] = []
    for generation_id in sorted(tree_import_gaps):
        for tree in tree_import_gaps[generation_id]:
            findings.append(
                HealthFinding(
                    code="tree_never_imported",
                    severity="warning",
                    summary=(
                        f"mutations to tree {tree} cannot have been under test in "
                        f"generation {generation_id}: no run of that generation ever "
                        f"imported {tree}, so the board scored code the loop never "
                        "changed"
                    ),
                    detail={
                        "generation_id": generation_id,
                        "tree": tree,
                        "recommendation": (
                            "check that the harness entrypoint imports the mutable "
                            f"tree ({tree}) rather than an installed copy under "
                            "another name, and that the board exercises the code "
                            "path the mutations target; the per-generation record is "
                            "generations/<gen>/harness_load.json"
                        ),
                    },
                )
            )
    return findings


def detect_attributable_entry_regression(
    entry_regressions: dict[str, dict[str, Any]] | None,
) -> list[HealthFinding]:
    """Surface entries a PROMOTED duel regressed on their own evidence
    (issue #130).

    ``entry_regressions`` is ``{entry_id: {parent_score, child_score,
    parent_drift_loss, child_drift_loss}}`` — the gate's
    :func:`zicato.tournament.gate.attributable_regression_detail` for a round
    whose verdict was ``promoted`` and whose
    ``GateOutcome.attributable_regressions`` was non-empty. The orchestrator
    threads it per round like :func:`detect_infra_outage` does its circuit
    trip; ``None`` / empty (every round that promoted cleanly, and every
    rejection — a rejected challenger is discarded, so nothing was baked in) is
    silent.

    A ``warning``. The gate promoted, correctly under the contract it was
    given: an ``aggregate``-scope contract PERMITS entry trades, and no rule
    reads per-entry drift at all. But the entry is now regressed in the
    champion lineage and every later round measures against it, while the
    promotion itself recorded an empty reason. This is the only place that
    says so.

    Deliberately NOT a veto, and there is no knob to make it one. Per-entry
    evidence is a single sample per side: at the board sizes this loop runs,
    one entry's drift moving 0.10 -> 0.60 is inside the range an A/A re-roll
    produces, so a gate built on it would reject real winners at a rate nobody
    has measured. The confirm-before-veto discipline (the measured noise floor
    preceded ``promote_margin`` advice; the placebo arm preceded reading the
    gate's discrimination) applies here too: this finding accumulates the
    evidence, and a gated veto — opt-in, thresholded against a measured
    per-entry floor — is registered for after that evidence exists.
    """
    if not entry_regressions:
        return []
    findings: list[HealthFinding] = []
    for entry_id in sorted(entry_regressions):
        row = entry_regressions[entry_id] or {}
        parent_drift = row.get("parent_drift_loss")
        child_drift = row.get("child_drift_loss")
        if isinstance(parent_drift, int | float) and isinstance(child_drift, int | float):
            movement = f"drift loss {float(parent_drift):.4g} -> {float(child_drift):.4g}"
        else:
            movement = (
                f"outcome score {_format_measure(row.get('parent_score'))} -> "
                f"{_format_measure(row.get('child_score'))}"
            )
        findings.append(
            HealthFinding(
                code="attributable_entry_regression",
                severity="warning",
                summary=(
                    f"board entry {entry_id} regressed ({movement}) in a round that "
                    "PROMOTED — the gate's rules did not read that movement, so the "
                    "regression is now the champion's baseline and the promotion "
                    "recorded no reason"
                ),
                detail={
                    "entry_id": entry_id,
                    "parent_score": row.get("parent_score"),
                    "child_score": row.get("child_score"),
                    "parent_drift_loss": parent_drift,
                    "child_drift_loss": child_drift,
                    "recommendation": (
                        f"population: the one board entry {entry_id}, on this round's "
                        "champion-vs-challenger duel. measured: its per-entry outcome "
                        "score and drift loss on both sides, read off the same "
                        "aggregates the gate decided on. compared against: the "
                        "per-entry monotonicity tolerance and the drift band "
                        "(child > 2x parent AND child > parent + 0.05). remedy: "
                        "inspect the entry's runs on both generations before the next "
                        "round measures against the new baseline; if entries must not "
                        "be traded away, set pass_rate_monotonicity_scope=per_entry, "
                        "which gates the OUTCOME axis. remedy safety: nothing here "
                        "vetoes, and per-entry drift stays ungated in every scope — a "
                        "single-sample per-entry movement is not yet distinguishable "
                        "from noise, so treat one finding as a prompt to look, and a "
                        "repeat across rounds on the same entry as a real regression"
                    ),
                },
            )
        )
    return findings


def _format_measure(value: Any) -> str:
    """Render a per-entry measurement for a summary line, or ``"unmeasured"``."""
    if isinstance(value, int | float) and not isinstance(value, bool):
        return f"{float(value):.4g}"
    return "unmeasured"


def detect_on_promote_hook_failed(
    on_promote_failure: tuple[str, str, str] | None,
) -> list[HealthFinding]:
    """Surface an adapter post-promotion hook that failed (issue #125).

    ``on_promote_failure`` is the ``(adapter_name, generation_id,
    exception_type)`` triple
    :func:`zicato.evolve.promote_hook.fire_on_promote` returns when an
    adapter's ``on_promote`` raised or blew the hook timeout. The hook is
    best-effort by contract — a failure never un-promotes the generation
    and never fails the round — which is exactly why it needs a finding:
    the champion advanced but whatever out-of-tree state the adapter
    keeps in step with the champion did NOT, and nothing else on any
    surface would say so. A ``warning``, not ``critical``: the loop's own
    records are correct and it can keep climbing; it is the target's
    external store the operator has to reconcile.

    ``None`` (every round with no hook, and every round whose hook
    succeeded) is silent — a runtime event threaded per round by the
    orchestrator, like :func:`detect_infra_outage`.
    """
    if on_promote_failure is None:
        return []
    adapter_name, generation_id, exception_type = on_promote_failure
    timed_out = exception_type == "TimeoutError"
    return [
        HealthFinding(
            code="on_promote_hook_failed",
            severity="warning",
            summary=(
                f"adapter {adapter_name!r} failed to commit the promotion of "
                f"{generation_id}: its on_promote hook raised {exception_type} — "
                "the generation IS promoted, but the adapter's out-of-tree state "
                "was not updated"
            ),
            detail={
                "adapter": adapter_name,
                "generation_id": generation_id,
                "exception_type": exception_type,
                "timed_out": timed_out,
                "recommendation": (
                    "the promotion is durable and the round is unaffected — "
                    f"reconcile {adapter_name!r}'s external side effect for "
                    f"{generation_id} MANUALLY (the promoted head is the last "
                    "promoted=true entry in lineage.json), then fix the hook; "
                    + (
                        "the hook exceeded its wall-clock ceiling, so check "
                        "whether the external store is reachable"
                        if timed_out
                        else "the full traceback is in the run's ERROR log"
                    )
                ),
            },
        )
    ]


def assess_loop_health(
    losses_by_generation: dict[str, list[LossProfile]],
    experiments: list[Any],
    board_entries: list[BoardEntry],
    epoch_id: str,
    config: HealthConfig | None = None,
    max_generations_per_contract: int | None = None,
    noise_floor: dict[str, Any] | None = None,
    promote_margin: float | None = None,
    evidence_gate_on: bool = True,
    preflight: dict[str, Any] | None = None,
    infra_outage: tuple[int, int] | None = None,
    token_clip: tuple[int, int] | None = None,
    tree_import_gaps: dict[str, tuple[str, ...]] | None = None,
    on_promote_failure: tuple[str, str, str] | None = None,
    preflight_gate: str = PREFLIGHT_GATE_DEFAULT,
    attributable_regressions: dict[str, dict[str, Any]] | None = None,
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
        thresholds. The orchestrator and the ``zicato health`` command
        build it from the workspace ``config.json``'s ``health`` block
        (:func:`zicato.config.health_config_from_workspace`); when
        omitted, the typed-config default applies. Resolved once here
        and passed to every threshold-using detector.
    max_generations_per_contract:
        The cadence ceiling from
        :attr:`~zicato.core.types.OverfittingConfig.max_generations_per_contract`,
        threaded through so :func:`detect_refresh_cadence` can surface a
        board-refresh recommendation. ``None`` (the default) disables the
        cadence detector entirely.
    noise_floor, promote_margin, evidence_gate_on:
        The epoch's measured A/A noise floor
        (:meth:`zicato.tournament.calibration.NoiseFloor.to_json` dict, or
        ``None`` when never measured), the contract's ``promote_margin``,
        and whether the Bradley--Terry evidence gate resolves to ON —
        threaded so :func:`detect_margin_below_noise_floor` can warn when
        the margin sits inside measured noise. ``noise_floor=None`` or
        ``promote_margin=None`` (the defaults) disable that detector.
    preflight:
        The epoch's persisted contract pre-flight verdict
        (:meth:`zicato.epoch.preflight.PreflightReport.to_json` dict, or
        ``None`` when never run) — threaded so
        :func:`detect_preflight_verdict` can keep a REFUSE/saturation
        verdict visible in every round's report. ``None`` (the default)
        disables that detector.
    preflight_gate:
        The operator's
        :attr:`~zicato.core.runtime.RuntimeConfig.preflight_gate` mode,
        threaded so :func:`detect_preflight_verdict` can grade a refusal
        ``critical`` only under the hard gate. Defaults to
        :data:`~zicato.core.runtime.PREFLIGHT_GATE_DEFAULT` (``"warn"``),
        matching the runtime default, so a caller that does not know the
        mode gets the recommend-only severity rather than one that can stop
        the loop.
    infra_outage:
        THIS round's endpoint-outage circuit trip, as an
        ``(infra_aborted_runs, threshold)`` pair — threaded by the
        orchestrator only for a round it DEFERRED on
        :attr:`~zicato.core.runtime.RuntimeConfig.infra_abort_round_threshold`
        (see :func:`detect_infra_outage`). ``None`` (the default) is
        silent.
    token_clip:
        THIS round's per-round token-budget clip, as a
        ``(tokens_spent, max_tokens_per_round)`` pair — threaded by the
        orchestrator only for a round the budget actually clipped (see
        :func:`detect_token_budget_clip`). ``None`` (the default) is
        silent.
    tree_import_gaps:
        ``{generation_id: (tree_basename, ...)}`` for the mutable trees no
        unit of that generation ever imported, read off the per-generation
        ``harness_load.json`` records — threaded so
        :func:`detect_tree_never_imported` can warn that a generation's
        mutations cannot have been under test (issue #110). ``None`` /
        empty (the default, and every healthy epoch) is silent.
    attributable_regressions:
        ``{entry_id: {parent/child score + drift}}`` for the entries THIS
        round's PROMOTED duel regressed on their own evidence — the gate's
        :func:`zicato.tournament.gate.attributable_regression_detail`,
        threaded per round by the orchestrator like ``infra_outage`` (see
        :func:`detect_attributable_entry_regression`). ``None`` / empty (the
        default, every rejection, and every clean promotion) is silent.
    on_promote_failure:
        THIS round's failed adapter post-promotion hook, as an
        ``(adapter_name, generation_id, exception_type)`` triple —
        threaded by the orchestrator only for a round whose promotion
        fired an ``on_promote`` that raised or timed out (see
        :func:`detect_on_promote_hook_failed`). ``None`` (the default,
        and every round with no hook) is silent.

    Returns
    -------
    LoopHealth
        ``healthy`` is ``True`` iff no finding has ``"warning"`` or
        ``"critical"`` severity.
    """
    health = _resolve_health_config(config)
    # The random-baseline placebo arm is a CALIBRATION probe rather than part of
    # the optimization stream: an always-rejected control fielded every
    # Nth round must not read as a stall, a flat-scoring window, or a
    # mined-out contract. Split it out — the stream detectors see only the
    # real experiments; the placebo experiments feed exactly one detector
    # (a promoted placebo is the gate-discrimination alarm). With the knob
    # off there are no placebo records and the split is the identity.
    placebo_experiments = [exp for exp in experiments if _is_placebo_experiment(exp)]
    if placebo_experiments:
        experiments = [exp for exp in experiments if not _is_placebo_experiment(exp)]
    findings: list[HealthFinding] = []
    findings.extend(detect_degenerate_scoring(experiments, health))
    findings.extend(detect_non_differentiating_entry(losses_by_generation))
    findings.extend(detect_flat_drift_signal(losses_by_generation))
    findings.extend(detect_no_expectations(board_entries, health))
    findings.extend(detect_dead_judge(losses_by_generation, board_entries))
    findings.extend(detect_stalled_loop(experiments, health))
    findings.extend(detect_generalization_gap(experiments, health))
    findings.extend(detect_refresh_cadence(experiments, max_generations_per_contract))
    findings.extend(detect_margin_below_noise_floor(noise_floor, promote_margin, evidence_gate_on))
    findings.extend(detect_preflight_verdict(preflight, preflight_gate))
    findings.extend(detect_placebo_promoted(placebo_experiments, promote_margin, noise_floor))
    findings.extend(detect_infra_outage(infra_outage))
    findings.extend(detect_token_budget_clip(token_clip))
    findings.extend(detect_tree_never_imported(tree_import_gaps))
    findings.extend(detect_attributable_entry_regression(attributable_regressions))
    findings.extend(detect_on_promote_hook_failed(on_promote_failure))

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
    "GENERALIZATION_GAP_WARN",
    "GENERALIZATION_GAP_CRIT",
    "HealthFinding",
    "LoopHealth",
    "assess_loop_health",
    "detect_attributable_entry_regression",
    "detect_degenerate_scoring",
    "detect_non_differentiating_entry",
    "detect_flat_drift_signal",
    "detect_no_expectations",
    "detect_dead_judge",
    "detect_stalled_loop",
    "detect_generalization_gap",
    "detect_infra_outage",
    "detect_noisy_judge",
    "detect_on_promote_hook_failed",
    "detect_placebo_promoted",
    "detect_preflight_verdict",
    "detect_refresh_cadence",
    "detect_token_budget_clip",
    "detect_tree_never_imported",
]
