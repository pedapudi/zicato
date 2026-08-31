"""Telemetry / loss types: the reducer's per-run output the scorer reads.

Split out of :mod:`zicato.core.types`; re-exported from there and from
:mod:`zicato.core` so existing import paths keep working.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from zicato.core.board import ExpectationKind

# ---------------------------------------------------------------------------
# Telemetry / loss
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DriftCount:
    """A count of drift events of one (kind, severity) pair within one run.

    Produced by the post-run reducer from a run's events JSONL. Multiple
    :class:`DriftCount` entries with the same :attr:`kind` but different
    :attr:`severity` may appear in one :class:`LossProfile` — the
    INTENT_DIVERGENCE kind fires at variable severity by design and the
    reducer keeps the buckets separate so severity-weighted scoring can
    do the right thing.

    Back-compat note
    ----------------
    :class:`DriftCount` is the original (drift-only) measurement unit.
    The generalised successor is :class:`MetricCount`, which carries an
    arbitrary namespaced metric name (``"drift:off_topic"``,
    ``"cost:input_tokens"``, ``"rubric:slide_structure"``, ...) and a
    float count. DriftCount is preserved verbatim so existing tests and
    JSON shapes that mention ``drift_counts`` keep working; the
    :meth:`MetricCount.from_drift_count` helper round-trips one into the
    other.

    Fields
    ------
    kind:
        Lowercase wire-canonical drift-kind string (see
        :mod:`zicato.core.drift_kinds`).
    severity:
        Goldfive's three-level severity scale.
    count:
        Number of drift events in this (kind, severity) bucket.
    """

    kind: str
    severity: Literal["info", "warning", "critical"]
    count: int


#: Severity literal for :class:`MetricCount`. Adds the empty string as a
#: "no severity" value for namespaces (cost, latency, output, ...) where
#: the drift three-bucket scale is meaningless.
MetricSeverity = Literal["info", "warning", "critical", ""]


@dataclass(frozen=True, slots=True)
class MetricCount:
    """Generic per-run metric measurement.

    Generalises :class:`DriftCount` so the same per-run unit can carry
    any namespaced metric the reducer / detectors / scorer cares about:
    drift kinds, cost (token counts, dollars), latency (p95 turn time),
    rubric scores, schema-failure counts, output-length stats, and so
    on. The namespace lives inside the :attr:`name` string as a
    colon-prefix (``"drift:off_topic"``, ``"cost:input_tokens"``,
    ``"rubric:slide_structure"``, ``"output:chars"``,
    ``"latency:p95_turn_ms"``).

    Drift becomes one namespace among many; :class:`DriftCount` stays as
    the back-compat surface and :meth:`from_drift_count` is the canonical
    promotion helper. The reducer continues to emit
    :attr:`LossProfile.drift_counts` and additionally exposes the
    superset view as :attr:`LossProfile.metric_counts` (everything in
    drift_counts is also present in metric_counts under the ``"drift:"``
    namespace, plus any other namespaces the reducer derived).

    Fields
    ------
    name:
        Namespaced metric name. Convention: ``"<namespace>:<key>"`` with
        a lowercase namespace prefix. Unnamespaced names (no colon) are
        legal but discouraged.
    severity:
        Severity bucket, or the empty string when the namespace has no
        natural severity (e.g. cost / latency).
    count:
        The measured value. Float rather than int so the same dataclass
        can carry counts (whole integers), rates (``[0.0, 1.0]``), scores
        (``[0.0, 5.0]``), and durations (milliseconds) without
        per-namespace dataclasses.
    """

    name: str
    severity: MetricSeverity = ""
    count: float = 0.0

    @classmethod
    def from_drift_count(cls, dc: DriftCount) -> MetricCount:
        """Promote a :class:`DriftCount` into a :class:`MetricCount`.

        The drift kind is prefixed with ``"drift:"`` to form the metric
        name. Severity and count carry over verbatim; the count is
        widened from ``int`` to ``float``.
        """
        return cls(
            name=f"drift:{dc.kind}",
            severity=dc.severity,
            count=float(dc.count),
        )


@dataclass(frozen=True, slots=True)
class JudgeLoss:
    """Per-judge loss attribution for one run.

    A custom in-run process judge fires a :class:`DriftDetected` of kind
    ``custom`` for each adverse verdict, paired with a
    ``JudgementEmitted`` carrying the judge's stable ``judge_name``. The
    reducer attributes each such drift to its authoring judge via
    :func:`zicato.telemetry.reducer._judge_attributed_kind` (folded into
    ``DriftCount.kind`` as ``"custom:<judge_name>"``). The aggregate
    drift_loss term that lives on :class:`LossProfile.drift_loss` already
    includes the per-judge contributions, but it does NOT preserve the
    per-judge attribution — every judge's contribution is summed into one
    scalar. :class:`JudgeLoss` carries that attribution out of the reducer
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
        aggregate ``drift_loss`` already sums in. Stored explicitly so a
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
    found nothing". An inline judge whose auxiliary endpoint 404s — a
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
        calls that reached the auxiliary LLM; python: the calls that
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
        means the matcher only produced a binary verdict; in that case
        the scalar and gate fall back to the binary ``passed`` bit, so a
        result with ``score=None`` is byte-identical to the pre-score
        behaviour. When a SCORER callable returns a float, the matcher
        clamps it to ``[0.0, 1.0]`` and records it here while ``passed``
        carries the thresholded bit for display / back-compat. A bool
        matcher leaves this ``None`` and the reducer derives the score as
        ``1.0`` / ``0.0`` from ``passed`` so the uniform mean still
        collapses to the binary pass-rate.
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

    A :class:`LossProfile` is produced by the post-run reducer after the
    goldfive JSONL has been written. Pattern detectors and tournament
    scoring consume :class:`LossProfile` instances; they never re-read
    the raw events. This decoupling lets us evolve event schemas
    upstream without touching scoring.

    The structure is flat by design — every field is a scalar, a tuple of
    scalars, or a tuple of small frozen dataclasses — so the reducer's
    output round-trips through JSON and can be diffed in the journal.

    Fields
    ------
    run_id:
        Unique id of the run this profile describes.
    entry_id:
        The :class:`BoardEntry.id` the run executed.
    generation_id, epoch_id:
        Lineage coordinates — which generation under which epoch produced
        this profile.
    drift_counts:
        Per (kind, severity) drift-event counts.
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
        Weighted scalar derived from :attr:`drift_counts` using the
        epoch's :class:`ScoringWeights`. Higher = worse.
    pass_fail:
        Derived from :attr:`expectation_result`; ``None`` when no
        expectation was attached. Allows pass-rate aggregation across
        the board to ignore entries without ground truth.
    score, metrics:
        Continuous per-entry outcome and its optional decomposition. See
        the field comments below — these are the prerequisite numbers the
        outcome-marginal proposer feedback (Capability 2) consumes.

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

    Generalised metric surface
    --------------------------
    metric_counts:
        Superset of :attr:`drift_counts` lifted into the namespaced
        :class:`MetricCount` shape. When the reducer populates it
        explicitly, ``metric_counts`` carries every namespace the
        reducer derived (drift kinds under ``"drift:"``, token counts
        under ``"cost:"``, output length under ``"output:"``, schema
        failures under ``"schema:"``, ...). When the field is left as
        the empty tuple — the back-compat default — :meth:`unified_metrics`
        synthesises it on the fly from :attr:`drift_counts` plus the
        first-class scalar fields.
    tokens_spent, output_chars, schema_failures:
        First-class scalar metrics promoted out of the
        ``metric_counts`` tuple because they show up so often in
        analysis. Single source of truth: the reducer ensures these
        scalars and their MetricCount mirror entries
        (``"cost:tokens_spent"``, ``"output:chars"``, ``"schema:failures"``)
        agree.
    adk_session_id:
        The ADK/goldfive session id for this run — the ``sessionId``
        envelope field present on every event in the run's
        ``events.jsonl``. goldfive keys its session views by this id;
        the harmonograf deep-link route is ``/#/session/<adk_session_id>``.
        Empty string when the events file is absent or carries no
        envelope ``sessionId``. Back-compat default: ``""`` so profiles
        written before this field was added load cleanly.
    match_id:
        The tournament matchup this run executed within — e.g.
        ``"rung0_m2"``, ``"rung1_m0"``, ``"racing-final"``. Stamped by
        the tournament runner once the run settles (the reducer/worker
        does not know it). Empty string for runs that ran outside a
        tagged matchup — a gauntlet duel (which goes through
        ``run_tournament``, not ``run_matchup``) or any ad-hoc run — and
        for profiles written before this field was added. The dashboard
        derives a ``rung`` label from it (see
        :func:`zicato.selection.strategy.rung_for_match_id`).
    """

    run_id: str
    entry_id: str
    generation_id: str
    epoch_id: str
    drift_counts: tuple[DriftCount, ...]
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
    # Generalised metric surface (back-compat: default empty; consumers
    # that want the merged view should call :meth:`unified_metrics`).
    metric_counts: tuple[MetricCount, ...] = ()
    tokens_spent: int = 0
    output_chars: int = 0
    schema_failures: int = 0
    # ADK/goldfive session id — carried on every event envelope; the
    # harmonograf deep-link route is /#/session/<adk_session_id>.
    # Back-compat default: "" so old profiles load without change.
    adk_session_id: str = ""
    # The tournament matchup this run ran within (e.g. "rung0_m2",
    # "racing-final"). Stamped by the tournament runner after the run
    # settles; "" for gauntlet / ad-hoc runs and for profiles written
    # before this field was added.
    match_id: str = ""
    # Per-judge loss attribution — empty tuple when no custom judge fired
    # against this run. The reducer sums each judge's ``custom``-kind
    # drift contributions (already attributed via ``custom:<judge_name>``)
    # and multiplies by the judge's weight; the aggregate ``drift_loss``
    # field already includes these contributions so this tuple is purely
    # the per-judge breakdown for downstream attribution. Back-compat
    # default: ``()`` so profiles written before this field was added
    # load cleanly.
    per_judge_loss: tuple[JudgeLoss, ...] = ()
    # Per-judge CALL-FAILURE provenance — empty tuple when every declared
    # judge's callable returned (the healthy case, and every profile
    # written before this field existed). ``per_judge_loss`` covers judges
    # that FIRED; this covers judges that were INVOKED AND RAISED, whose
    # silence is an error artifact rather than a verdict. Stamped by the
    # worker from the process-wide register
    # (:func:`zicato.judge_runtime.error_register.judge_error_snapshot`)
    # at profile-write time; read by
    # :func:`zicato.health.diagnostics.detect_dead_judge`. Back-compat
    # default: ``()`` so existing loss.json files load unchanged.
    judge_errors: tuple[JudgeError, ...] = ()
    # Carried-over (cached) provenance. ``cached`` is ``True`` when this
    # profile was NOT produced by a live run in its own epoch but
    # MATERIALISED from a prior evaluation — the champion carried forward
    # into a new epoch (baseline-seed reuse) or a fast-mode reuse. The
    # per-board scalar (``drift_loss`` / ``pass_fail``) is the carried
    # value; ``source_epoch`` / ``source_run`` name where it came from so
    # the champion is consistent with the challengers (both materialised
    # per board, distinguished only by this provenance) and the index
    # never double-counts a cached champion as a fresh evaluation.
    # Back-compat default: ``cached=False`` / empty sources for every
    # freshly-run profile, so existing loss.json files load unchanged.
    cached: bool = False
    source_epoch: str = ""
    source_run: str = ""
    # Continuous per-entry outcome. ``score`` is the entry's quality in
    # ``[0.0, 1.0]`` (F1, similarity, a partial-credit rubric, ...).
    # ``None`` (the back-compat default) means the entry had only a binary
    # verdict; downstream scoring then derives the score from ``pass_fail``
    # (True->1.0, False->0.0, None->excluded), so a profile with
    # ``score=None`` is byte-identical to the pre-score binary path. The
    # reducer always populates this when an expectation fired (a bool
    # matcher yields 1.0/0.0, a float scorer yields the clamped value), so
    # the field is ``None`` ONLY on profiles written before this field
    # existed or on entries with no expectation. ``metrics`` carries any
    # per-entry decomposition the scorer exposed (e.g.
    # ``{"precision": .., "recall": ..}``) out to ``loss.json``; ``None``
    # when the scorer exposed none. Both are OUTPUT only — they are not
    # contract fields and never enter the contract hash.
    score: float | None = None
    metrics: dict[str, float] | None = None
    # Scoring provenance (issue #19) — which scoring path produced
    # ``drift_loss``. ``None`` (the back-compat default) and ``"builtin"``
    # both mean the extracted built-in formula produced it; later phases
    # enrich this with ``"transform:..."`` / ``"plugin:..."`` / a fail-open
    # ``"builtin (fallback: ...)"`` marker. OUTPUT only — never a contract
    # field, never enters the contract hash. A ``loss.json`` written before
    # this field existed loads with ``scoring_provenance=None``.
    scoring_provenance: str | None = None
    # Abort provenance — WHY a synthesised worst-case profile was recorded,
    # so loop-health can tell an honest wall-clock-budget exhaustion from an
    # INFRA abort (a parent/supervisor kill or a worker crash). ``None`` (the
    # back-compat default) and ``""`` both mean "not an aborted profile" — a
    # cleanly-reduced run carries no cause. The synthesised values are a
    # small open vocabulary:
    #
    #   * ``"budget_exhausted"`` — the run genuinely hit its wall-clock
    #     budget (the worker's own cooperative budget fired, or a matchup-
    #     level budget skipped the un-run unit). This is the ONLY cause the
    #     cache may persist: re-running would re-hit the same budget.
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
    # Only the genuine budget cause is cache-persistable; an infra cause is
    # NOT cached so a transient blip never poisons a board unit's score for
    # the rest of the epoch (re-running re-attempts the unit). Readers MUST
    # tolerate absence (``None`` / ``""``) — every profile written before
    # this field existed, and every freshly-reduced (non-aborted) run, omits
    # it. OUTPUT only — never a contract field, never enters the contract hash.
    abort_cause: str | None = None
    # WHETHER this run reached a non-success terminal state — killed, crashed,
    # harness-exception, emulator-leak-aborted, or wall-clock exhausted. It is
    # the fact the ``failure:not_completed`` channel member scores, so it must
    # be first-class: :attr:`not_completed_reason` is legitimately ``None`` for
    # an abort whose adapter supplied no reason, and reading its absence as
    # "completed" would hand a crashed run the best possible score. ``False``
    # is the healthy case and the default. OUTPUT only — never a contract
    # field, never enters the contract hash.
    not_completed: bool = False
    # Not-completed provenance — WHY the reducer scored this run worst-case
    # (``run_not_completed``: the ``failure:`` channel's fixed
    # not-completed magnitude plus a ``task_failure_ratio`` floored to 1.0).
    # The value is the adapter's own
    # :attr:`RunResult.abort_reason`: ``"harness_exception:{type}"``,
    # ``"unsupported_kind:{kind}"``, an emulator abort, the worker's
    # wall-clock reason, or a custom adapter string. Without it the charge
    # is unattributable — the profile carries a large failure-channel term
    # with an empty ``drift_counts`` and nothing naming its cause.
    #
    # Distinct from :attr:`abort_cause`, which is the CACHE-eligibility
    # signal (:func:`is_infra_abort_cause` reads any non-budget value as an
    # infra abort and suppresses the persist). An adapter-returned reason
    # belongs here and NEVER there: stamping it on ``abort_cause`` would
    # stop a crashing run's worst-case loss from ever being cached, turning
    # a scored failure into "no evidence". ``None`` (the back-compat
    # default) means the run completed, or the profile predates the field.
    # OUTPUT only — never a contract field, never enters the contract hash.
    not_completed_reason: str | None = None
    # Wall-clock span of the run that produced this profile — ISO-8601 UTC
    # strings (:func:`datetime.datetime.now` under ``UTC``), stamped by the
    # worker around the drive of the board unit. :attr:`runtime_ms` gives a
    # duration but no position, so two units cannot be ordered against each
    # other, placed on a timeline, or shown as concurrent; these fields
    # supply the position. ``None`` (the back-compat default) means no span
    # was measured — the profile predates the fields, or it is a synthesised
    # worst-case for a run that never reported one (a killed worker, a unit
    # skipped for budget). NOT "epoch zero", which is why the default is not
    # ``""``/``0``. A cached unit carries the times of the run that produced
    # it (nothing rewrites them), which is what makes ``cached`` readable as
    # "did not run this round". OUTPUT only — never contract fields, never
    # enter the contract hash.
    started_at: str | None = None
    ended_at: str | None = None

    def unified_metrics(self) -> tuple[MetricCount, ...]:
        """Return the merged metric view across drift_counts + metric_counts.

        Always returns at least every :attr:`drift_counts` entry lifted
        into a :class:`MetricCount` under the ``"drift:"`` namespace.
        When :attr:`metric_counts` is non-empty, its entries are
        appended after the drift-promoted ones; the caller can dedupe
        on ``(name, severity)`` if they care, but the reducer is
        responsible for not emitting the same drift entry in both
        tuples — :attr:`metric_counts` is a superset when populated.

        When :attr:`metric_counts` is empty, the helper also synthesises
        :class:`MetricCount` entries for the first-class scalar fields
        (``tokens_spent``, ``output_chars``, ``schema_failures``) so
        downstream consumers see a uniform view regardless of how the
        profile was constructed.

        Three channels are then DERIVED from first-class fields, always and
        regardless of how the profile was built, so that a synthesised or
        hand-built profile carries them exactly as a reduced one does:

        * ``judge:<name>`` — one per :attr:`per_judge_loss` entry, carrying
          that judge's already-per-judge-weighted loss. This is the only
          route custom judges take into the scalar; their ``drift:custom``
          mirrors are excluded from the generic namespace aggregation
          so the two cannot double-count.
        * ``failure:tasks`` / ``failure:not_completed`` — the run-outcome
          facts (:attr:`task_failure_ratio`, :attr:`not_completed`). Emitted
          even at zero so the key set does not depend on whether a run went
          wrong.
        * ``runtime:seconds`` — :attr:`runtime_ms` in seconds, kept out of
          ``latency:`` because that namespace's coefficient is calibrated
          for millisecond percentiles.

        The derived entries carry the RAW measurement; their contract
        coefficients (``task_failure_weight`` / ``not_completed_weight``
        within the channel, ``namespace_weights`` across channels) are
        applied by :func:`zicato.tournament.scoring.aggregate_namespaced_metrics`.
        A name already present from ``metric_counts`` wins — the derivation
        never overwrites a measured entry.
        """
        out: list[MetricCount] = [MetricCount.from_drift_count(dc) for dc in self.drift_counts]
        if self.metric_counts:
            # Caller (the reducer) has populated the superset view
            # explicitly; trust it but skip duplicates of the drift
            # entries we already emitted.
            seen = {(mc.name, mc.severity) for mc in out}
            for mc in self.metric_counts:
                if (mc.name, mc.severity) in seen:
                    continue
                out.append(mc)
                seen.add((mc.name, mc.severity))
        else:
            # Synthesise the first-class scalars only when the caller
            # hasn't given us a richer view. Avoids double-counting when
            # the reducer already wrote them into metric_counts.
            if self.tokens_spent:
                out.append(
                    MetricCount(
                        name="cost:tokens_spent", severity="", count=float(self.tokens_spent)
                    )
                )
            if self.output_chars:
                out.append(
                    MetricCount(name="output:chars", severity="", count=float(self.output_chars))
                )
            if self.schema_failures:
                out.append(
                    MetricCount(
                        name="schema:failures", severity="", count=float(self.schema_failures)
                    )
                )

        # Derived channels — see the docstring. Deduped by NAME (not by
        # ``(name, severity)``): these are per-run scalars with no severity,
        # and a measured entry of the same name is authoritative.
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


#: The single abort cause that is a GENUINE wall-clock-budget exhaustion —
#: the only cause for which a synthesised worst-case :class:`LossProfile` may
#: be persisted to the unit cache (re-running would re-hit the same budget,
#: so caching it is correct and saves a wasted re-run). Every OTHER cause is
#: an INFRA abort (a parent/supervisor kill or a worker crash) that must NOT
#: be cached, so a transient blip cannot poison a board unit's score for the
#: rest of the epoch.
BUDGET_ABORT_CAUSE = "budget_exhausted"


def is_infra_abort_cause(abort_cause: str | None) -> bool:
    """Return ``True`` iff ``abort_cause`` names a non-cacheable INFRA abort.

    A profile is an infra abort when it carries an ``abort_cause`` that is
    NOT the genuine :data:`BUDGET_ABORT_CAUSE` wall-clock exhaustion — i.e. a
    parent/supervisor kill, a worker crash, a prepare failure, or an
    unreadable result. The empty string / ``None`` means "not an aborted
    profile at all" and is therefore NOT an infra abort (a cleanly-reduced
    run is always cacheable).
    """
    return bool(abort_cause) and abort_cause != BUDGET_ABORT_CAUSE
