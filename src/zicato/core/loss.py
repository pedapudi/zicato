"""Named measurements, task outcomes, and provenance consumed by scoring."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from zicato.core.board import ExpectationKind
from zicato.core.measurement import UNKNOWN_SEED, MeasurementDraw

# ---------------------------------------------------------------------------
# Telemetry / loss
# ---------------------------------------------------------------------------


#: Severity literal for :class:`MetricCount`. Adds the empty string as a
#: "no severity" value for namespaces (cost, latency, output, ...) where
#: the drift three-bucket scale is meaningless.
MetricSeverity = Literal["info", "warning", "critical", ""]


@dataclass(frozen=True, slots=True)
class MetricCount:
    """A named measurement with a severity bucket and numeric value.

    Names use a namespace prefix, such as ``drift:off_topic`` or
    ``cost:tokens_spent``. Drift measurements preserve their event severity;
    cost, duration, and rubric measurements use an empty severity.
    """

    name: str
    severity: MetricSeverity = ""
    count: float = 0.0


@dataclass(frozen=True, slots=True)
class JudgeLoss:
    """Per-judge loss attribution for one run.

    A custom in-run process judge fires a :class:`DriftDetected` of kind
    ``custom`` for each adverse verdict, paired with a
    ``JudgementEmitted`` carrying the judge's stable ``judge_name``. The
    reducer attributes each such drift to its authoring judge via
    :func:`zicato.telemetry.reducer._judge_attributed_kind` (folded into
    ``MetricCount.name`` as ``"drift:custom:<judge_name>"``).
    :class:`JudgeLoss` carries the weighted attribution out of the reducer
    so downstream consumers (the analyzer's per-judge drift-attribution
    section, the analytical index's ``judge_losses`` table) can answer
    "which judges drove this run's loss" without re-walking ``events.jsonl``.

    Fields
    ------
    judge_name:
        Stable per-judge identity (the ``name`` attribute of a
        :class:`zicato.board.judges.Judge`). Mirrors the key under
        :attr:`ScoringWeights.per_judge_weights`. The bare ``""``
        (empty string) names the catch-all bucket for unattributed
        ``custom``-kind drifts the reducer could not pair with a
        ``JudgementEmitted``.
    raw_loss:
        The judge's unweighted drift contribution — the
        severity-weighted sum of the judge's ``custom`` drift counts:
        ``sum(severity_weights[c.severity] * c.count for c in
        judge_drifts)``. Comparable across judges within the same epoch.
    weight:
        The judge's multiplier (:attr:`ScoringWeights.per_judge_weights`
        value, falling back to :attr:`ScoringWeights.default_judge_weight`).
        Preserved on the profile so the ingest path does not have to
        re-read scoring.json to recover the multiplier.
    weighted_loss:
        ``raw_loss * weight`` — the per-judge contribution that the
        ``judge:`` scoring channel sums in. Stored explicitly so a
        round-trip through JSON does not lose precision.
    """

    judge_name: str
    raw_loss: float
    weight: float
    weighted_loss: float


@dataclass(frozen=True, slots=True)
class JudgeError:
    """Per-judge CALL-FAILURE provenance for one run.

    :class:`JudgeLoss` covers judges that FIRED. This covers the third
    outcome a judge can have: the judge's callable RAISED.

    Without this record that outcome is indistinguishable from "fired and
    found nothing". An inline judge whose evaluation endpoint 404s — a
    misconfigured judge model, a revoked key, a transient outage — returns
    an empty verdict by hard contract, because a judge must never crash a
    run. goldfive emits no ``JudgementEmitted`` for an empty verdict. So a
    judge that raised on every invocation reads byte-identically, in both
    ``loss.json`` and ``events.jsonl``, to one that ran and found nothing,
    and the only other trace is a WARNING in a log that rotates.

    This tuple is that trace made durable: zicato's judge boundary
    (:mod:`zicato.judge_runtime.error_register`) counts invocations and
    errors per judge name for the worker process, and the worker stamps
    the snapshot onto the profile it writes. Loop health reads it to tell
    "raised on 34 of 34 invocations" — a broken endpoint, actionable —
    apart from "never fired", which routes the operator into a board
    audit of a judge that was never given a chance to answer.

    Fields
    ------
    judge_name:
        Stable per-judge identity — the ``name`` of the
        :class:`~zicato.core.types.JudgeSpec` the board declared, the
        same key :attr:`JudgeLoss.judge_name` and
        ``ScoringWeights.per_judge_weights`` use.
    invocations:
        How many times this run called the judge's callable (inline: the
        calls that reached the evaluation LLM; python: the calls that
        reached the operator's code). Observation points with nothing to
        judge — an empty reasoning trace — are not invocations.
    errors:
        How many of those invocations raised. ``errors == invocations``
        is a judge that never once produced a verdict; ``0 < errors <
        invocations`` is a flaky endpoint whose zero-drift signal is
        partly an artifact.
    last_error_type:
        The exception TYPE name of the most recent failure
        (``"RuntimeError"``, ``"TimeoutError"``, ...) — enough to route
        the operator at the right config without copying an endpoint's
        error text (which can carry request ids / URLs) into a scored,
        indexed artifact. The verbatim message rides the reflection
        sidecar (``judge_io.jsonl``'s error entry) instead.
    """

    judge_name: str
    invocations: int
    errors: int
    last_error_type: str = ""


@dataclass(frozen=True, slots=True)
class ExpectationResult:
    """The outcome of evaluating a :class:`BoardEntry`'s expectation.

    Fields
    ------
    kind:
        The matcher kind that produced this result (same value as the
        originating :class:`Expectation.kind`). Typed as the
        :class:`ExpectationKind` enum; because that enum subclasses
        ``str``, a producer may still pass the bare wire token and it
        compares equal to the matching member.
    passed:
        ``True`` iff the matcher accepted the run.
    detail:
        Optional human-readable explanation (e.g. regex match position,
        judge rationale). Empty string when the matcher had nothing
        useful to say. Stored to give the journal something concrete to
        render alongside a pass/fail bit.
    score:
        Optional CONTINUOUS per-entry quality in ``[0.0, 1.0]`` — F1,
        similarity, a partial-credit rubric, etc. ``None`` (the default)
        means the matcher produced a binary verdict. The reducer then
        derives ``1.0`` or ``0.0`` from ``passed``. A scorer's numeric
        return is clamped to ``[0.0, 1.0]``; ``passed`` carries its
        thresholded verdict for display and pass-rate reporting.
    metrics:
        Optional per-entry metric carrier (e.g.
        ``{"precision": 0.3, "recall": 0.6}``) a scorer may populate
        alongside its scalar ``score``. ``None`` (the default) when the
        matcher exposed no decomposition. Carried out to ``loss.json`` so
        downstream aggregation (the proposer's failure-mode profile) can
        read precision/recall as numbers without re-running the scorer.
    """

    kind: ExpectationKind
    passed: bool
    detail: str = ""
    score: float | None = None
    metrics: dict[str, float] | None = None


@dataclass(frozen=True, slots=True)
class LossProfile:
    """The reducer's per-run output — the contract scoring reads from.

    A :class:`LossProfile` is produced by the post-run reducer from the
    selected telemetry dialect. Pattern detectors and tournament
    scoring consume :class:`LossProfile` instances; they never re-read
    the raw events. This decoupling lets us evolve event schemas
    upstream without touching scoring.

    Declared fields round-trip through the canonical JSON codec, including
    nested expectation results and named metric decompositions.

    Fields
    ------
    run_id:
        Unique id of the run this profile describes.
    entry_id:
        The :class:`BoardEntry.id` the run executed.
    generation_id, epoch_id:
        Lineage coordinates — which generation under which epoch produced
        this profile.
    plan_revisions:
        Number of plan-revision events observed. A high count generally
        indicates the steerer worked hard; whether that is "good" or
        "bad" depends on outcome and the operator's rubric.
    task_failure_ratio:
        Ratio of fatally-failed tasks to total tasks the run produced.
        Range ``[0.0, 1.0]``.
    runtime_ms:
        Total wall-clock duration in milliseconds.
    wall_clock_budget_exceeded:
        ``True`` iff the run hit :attr:`BoardEntry.wall_clock_budget_seconds`
        and was force-aborted. When true, scoring treats this run as
        worst-case for the entry.
    expectation_result:
        Result of evaluating the entry's expectation, or ``None`` when
        the entry had no expectation. Note: this is allowed to be ``None``
        even on entries that DID have an expectation but the run was
        aborted before the expectation could fire — the reducer records
        that distinction via :attr:`wall_clock_budget_exceeded`.
    drift_loss:
        Weighted scalar derived from named drift measurements using the
        epoch's :class:`ScoringWeights`. Higher = worse.
    pass_fail:
        Derived from :attr:`expectation_result`; ``None`` when no
        expectation was attached. Allows pass-rate aggregation across
        the board to ignore entries without ground truth.
    score, metrics:
        Continuous per-entry outcome and its optional decomposition. See
        field comments below. Proposer feedback consumes these values to
        identify which task outcomes changed.

    Multi-turn extras (single-turn entries leave these as ``None``)
    ----------------------------------------------------------------
    turns_completed:
        Number of conversational turns the run executed before
        terminating (whether by ``stop_when``, ``max_turns``, or abort).
    memory_failure_count:
        Zicato-derived signal: number of times across the conversation
        the inner agent re-asked something the simulated user had
        already answered. Computed by the reducer rather than by goldfive.
    context_loss_count:
        Zicato-derived signal: number of times the inner agent appeared
        to forget a fact established earlier in the conversation.
        Heuristic; same multi-turn-pattern detector as
        :attr:`memory_failure_count`.

    Named measurements
    ------------------
    metric_counts:
        Measured values keyed by namespace, metric name, and severity.
        Drift metrics use ``drift:<kind>``. Cost, output, and schema values
        use their respective namespaces. Scoring reads this tuple directly.
    tokens_spent, output_chars, schema_failures:
        Integer counters used by runtime budgets and display. The reducer
        records their named metric values for scoring. Replicate folds retain
        fractional metric means while rounding these display counters.
    adk_session_id:
        The ADK/goldfive session id for this run — the ``sessionId``
        envelope field present on every event in the run's
        ``events.jsonl``. goldfive keys its session views by this id;
        the harmonograf deep-link route is ``/#/session/<adk_session_id>``.
        Empty string when the events file is absent or carries no
        envelope ``sessionId``.
    match_id:
        The tournament matchup this run executed within — e.g.
        ``"rung0_m2"``, ``"rung1_m0"``, ``"racing-final"``. Stamped by
        the tournament runner once the run settles (the reducer/worker
        does not know it). Empty string for runs that ran outside a
        tagged matchup — a gauntlet duel (which goes through
        ``run_tournament``, not ``run_matchup``) or any ad-hoc run. The dashboard
        derives a ``rung`` label from it (see
        :func:`zicato.selection.strategy.rung_for_match_id`).
    """

    run_id: str
    entry_id: str
    generation_id: str
    epoch_id: str
    plan_revisions: int
    task_failure_ratio: float
    runtime_ms: int
    wall_clock_budget_exceeded: bool
    expectation_result: ExpectationResult | None
    drift_loss: float
    pass_fail: bool | None
    # Multi-turn extras
    turns_completed: int | None = None
    memory_failure_count: int | None = None
    context_loss_count: int | None = None
    # Named measurements; outcome-derived scoring channels are computed separately.
    metric_counts: tuple[MetricCount, ...] = ()
    tokens_spent: int = 0
    output_chars: int = 0
    schema_failures: int = 0
    # ADK/goldfive session id — carried on every event envelope; the
    # harmonograf deep-link route is /#/session/<adk_session_id>.
    # Empty when the telemetry has no session identifier.
    adk_session_id: str = ""
    # The tournament matchup this run ran within (e.g. "rung0_m2",
    # "racing-final"). Stamped by the tournament runner after the run
    # settles; empty when the run has no tagged matchup.
    match_id: str = ""
    # Per-judge loss attribution — empty tuple when no custom judge fired
    # against this run. The reducer sums each judge's ``custom``-kind
    # drift contributions (already attributed via ``custom:<judge_name>``)
    # and multiplies by the judge's weight. These contributions form the
    # judge scoring channel and do not enter the drift channel a second time.
    per_judge_loss: tuple[JudgeLoss, ...] = ()
    # Failed judge calls, recorded separately from adverse judge verdicts.
    # Empty when no judge raised. The worker captures these counters from
    # judge_error_snapshot; health diagnostics use them to detect a judge
    # whose missing verdicts result from errors.
    judge_errors: tuple[JudgeError, ...] = ()
    # Reused measurements retain their source epoch and run. This lets
    # readers distinguish carried observations from fresh execution and
    # prevents the index from counting reuse as an independent measurement.
    # Freshly executed profiles have cached=False and empty source fields.
    cached: bool = False
    source_epoch: str = ""
    source_run: str = ""
    # Per-entry quality in [0.0, 1.0]. The reducer derives 1.0/0.0 for a
    # binary verdict and preserves a numeric scorer's clamped value.
    # None means no numeric outcome was recorded; scoring uses pass_fail
    # when available and excludes an entry with neither observation.
    # metrics carries the scorer's optional decomposition. Both fields
    # record measured outcomes and are excluded from contract identity.
    score: float | None = None
    metrics: dict[str, float] | None = None
    # The scoring path that produced drift_loss: builtin, transform,
    # plugin, or a builtin fallback with its failure reason. None means
    # no path was recorded, including synthesized failure profiles.
    scoring_provenance: str | None = None
    # Abort provenance — WHY a synthesised worst-case profile was recorded,
    # so loop-health can tell an honest wall-clock-budget exhaustion from an
    # INFRA abort (a parent/supervisor kill or a worker crash). ``None`` and
    # ``""`` mean no infrastructure or budget cause was recorded. The
    # synthesised values are a
    # small open vocabulary:
    #
    #   * ``"budget_exhausted"`` — the run genuinely hit its wall-clock
    #     budget. A matchup may also assign this cause to a skipped unit;
    #     execution_started=False excludes that omission from evidence.
    #   * ``"parent_kill"`` — the parent killed a wedged worker that blew
    #     past ``budget + grace`` without self-terminating (an infra abort).
    #   * ``"gone_no_result"`` — the worker vanished with no result file
    #     (a supervisor SIGKILL past the deadline, or a hard crash before it
    #     could write — an infra abort).
    #   * ``"nonzero_exit:{code}"`` — the worker process exited non-zero
    #     with no usable result (a crash — an infra abort).
    #   * ``"prepare_failed"`` — the run could not be prepared for a
    #     subprocess (the per-run snapshot copytree failed: disk full,
    #     source missing — an infra abort).
    #   * ``"result_unreadable"`` — the worker reported a clean exit but its
    #     ``loss.json`` was missing/corrupt (an infra abort).
    #
    # Executed budget failures may enter the cache; infrastructure failures
    # remain retryable. This diagnostic does not determine task completion:
    # adapter failures use not_completed and not_completed_reason below.
    abort_cause: str | None = None
    # WHETHER this run reached a non-success terminal state — killed, crashed,
    # harness-exception, emulator-leak-aborted, or wall-clock exhausted. It is
    # the fact the ``failure:not_completed`` channel member scores, so it must
    # be recorded separately: :attr:`not_completed_reason` can be ``None`` for
    # an abort whose adapter supplied no reason, and reading its absence as
    # "completed" would hand a crashed run the best possible score. ``False``
    # is the healthy case and the default. OUTPUT only — never a contract
    # field, never enters the contract hash.
    not_completed: bool = False
    # The adapter's reason for not completing, such as a harness exception,
    # unsupported entry kind, or wall-clock limit. None means no reason was
    # supplied; not_completed remains the completion authority. Adapter
    # reasons belong here because abort_cause controls infrastructure retry
    # eligibility and would suppress caching of a measured task failure.
    not_completed_reason: str | None = None
    # ISO-8601 UTC worker timestamps position the run on a timeline.
    # None means the worker reported no timestamp, as with a killed worker
    # or skipped unit. Cached profiles retain the source run's timestamps.
    started_at: str | None = None
    ended_at: str | None = None
    # Whether the task started. False records an unmeasured scheduling
    # omission. None means no explicit start observation was recorded;
    # has_execution_evidence applies the timing and token evidence policy.
    # A replicate fold is False if any requested draw
    # never started; its partial measurement cannot support a decision.
    execution_started: bool | None = None
    measurement: MeasurementDraw | None = None
    # A fold retains every source draw; None marks a source without identity.
    # Such a fold cannot claim that all inputs share a known execution seed.
    source_measurements: tuple[MeasurementDraw | None, ...] = ()

    def scoring_metrics(self) -> tuple[MetricCount, ...]:
        """Measured metrics plus judge, failure, and runtime scoring channels.

        Derived channels retain their raw units; scoring applies contract
        coefficients. A measured entry of the same name takes precedence.
        """
        out = list(self.metric_counts)
        derived: list[MetricCount] = [
            MetricCount(name=f"judge:{jl.judge_name}", severity="", count=float(jl.weighted_loss))
            for jl in self.per_judge_loss
        ]
        derived.append(
            MetricCount(name="failure:tasks", severity="", count=float(self.task_failure_ratio))
        )
        derived.append(
            MetricCount(
                name="failure:not_completed", severity="", count=1.0 if self.not_completed else 0.0
            )
        )
        derived.append(
            MetricCount(name="runtime:seconds", severity="", count=self.runtime_ms / 1000.0)
        )
        seen_names = {mc.name for mc in out}
        for mc in derived:
            if mc.name in seen_names:
                continue
            out.append(mc)
            seen_names.add(mc.name)
        return tuple(out)


#: Budget exhaustion is cache-eligible only with execution evidence.
#: Other recorded causes identify infrastructure failures that remain retryable.
BUDGET_ABORT_CAUSE = "budget_exhausted"


def is_infra_abort_cause(abort_cause: str | None) -> bool:
    """Identify an infrastructure cause that prevents caching the profile.

    Empty or absent causes do not establish task completion or execution;
    those facts have their own fields and admission checks.
    """
    return bool(abort_cause) and abort_cause != BUDGET_ABORT_CAUSE


def has_execution_evidence(record: LossProfile | Mapping[str, Any]) -> bool:
    """Apply one execution policy to decoded profiles and canonical JSON readers.

    An explicit start observation takes precedence. Without it, budget failures
    need timing or token evidence; other profiles pass this execution check.
    Measurement identity and cache eligibility are checked separately.
    """

    def value(name: str) -> Any:
        return record.get(name) if isinstance(record, Mapping) else getattr(record, name)

    started = value("execution_started")
    if started is not None:
        return started is True
    if value("abort_cause") != BUDGET_ABORT_CAUSE:
        return True
    return bool(value("started_at") or value("ended_at")) or any(
        float(value(name) or 0) > 0 for name in ("runtime_ms", "tokens_spent")
    )


def validate_loss_identity(
    record: LossProfile | Mapping[str, Any],
    *,
    epoch_id: str,
    generation_id: str,
    entry_id: str,
    measurement: MeasurementDraw | None,
) -> None:
    """Require recorded coordinates to agree with the requested measurement.

    Audit mappings without known seed provenance may omit coordinates.
    Recorded coordinates must agree wherever present. Known seeds require
    complete coordinates and the canonical runtime identifier for that draw.
    """
    from zicato.core.workspace import run_id_for_unit  # noqa: PLC0415

    missing = object()

    def value(name: str) -> Any:
        return record.get(name, missing) if isinstance(record, Mapping) else getattr(record, name)

    actual = tuple(value(name) for name in ("epoch_id", "generation_id", "entry_id"))
    expected = (epoch_id, generation_id, entry_id)
    if any(
        found is not missing and found != wanted
        for found, wanted in zip(actual, expected, strict=False)
    ):
        raise ValueError("recorded loss coordinates conflict with the requested cell")
    if measurement is not None and measurement.base_seed is not UNKNOWN_SEED:
        run_id = run_id_for_unit(
            generation_id, entry_id, measurement.replicate_index, base_seed=measurement.base_seed
        )
        if actual != expected or value("run_id") != run_id:
            raise ValueError("recorded loss runtime identity conflicts with the requested draw")


def capture_matches_loss(body: Mapping[str, Any], expected: LossProfile | None) -> bool:
    """Require complete capture identity for a loss with recorded seed provenance.

    Unpaired reads and unknown seeds remain available for audit;
    accepting their capture bytes does not establish a measurement match.
    """
    try:
        draw = MeasurementDraw.from_json(body["measurement"]) if "measurement" in body else None
    except (TypeError, ValueError):
        return False
    if expected is None or expected.measurement is None:
        return True
    if expected.measurement.base_seed is UNKNOWN_SEED:
        return True
    return (
        bool(expected.run_id)
        and draw == expected.measurement
        and body.get("run_id") == expected.run_id
    )
