"""Runtime-config types: the model-agnostic LLM shape + the runtime binding.

Split out of :mod:`zicato.core.types`; re-exported from there and from
:mod:`zicato.core` so existing import paths keep working.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Runtime config
# ---------------------------------------------------------------------------


#: The model-agnostic LLM-call shape used everywhere in zicato.
#:
#: Mirrors goldfive's call_llm surface: ``(system, user, model) ->
#: response``. The ``model`` parameter is a free-form string the caller
#: passes through; concrete implementations interpret it (route to a
#: provider, look up credentials, etc.). Zicato never inspects or
#: switches on ``model``.
CallLLM = Callable[[str, str, str], Awaitable[str]]

#: Default first-deferral backoff (seconds) for the endpoint-outage circuit
#: (:attr:`RuntimeConfig.infra_backoff_base_s`). Module-level so the evolve
#: loop — which reads the raw workspace ``runtime`` block without building a
#: full :class:`RuntimeConfig` — shares one source of truth with the factory.
INFRA_BACKOFF_BASE_S_DEFAULT: float = 30.0

#: Default ceiling (seconds) on the exponential infra backoff
#: (:attr:`RuntimeConfig.infra_backoff_cap_s`). See the base default above.
INFRA_BACKOFF_CAP_S_DEFAULT: float = 480.0

#: Valid values for :attr:`RuntimeConfig.preflight_gate`, weakest first.
#: ``"off"`` — do not run the achievable-signal pre-flight (UNLESS a legacy
#: ``contract_preflight: K`` key explicitly requests it, preserving pre-#84
#: opt-in behaviour); with no such key — the common case, incl. deterministic
#: oracles — ``"off"`` runs no pre-flight at all; ``"warn"`` (the DEFAULT) —
#: measure it once per epoch at evolve start and LOUDLY warn on a
#: below-noise-floor / saturated verdict, but never stop; ``"refuse"`` —
#: additionally HARD-STOP the run before spending rounds when the verdict is
#: ``refuse``. Cost: ``"warn"``/``"refuse"`` add ~K+1 champion board
#: evaluations (the A/A draws + one degraded probe) once per epoch at evolve
#: start; on a real endpoint that is real budget, counted against round 0.
PREFLIGHT_GATE_MODES: tuple[str, ...] = ("off", "warn", "refuse")

#: Default pre-flight gate mode — measure + warn, never block (recommend-only).
PREFLIGHT_GATE_DEFAULT: str = "warn"

#: Default CEILING on how many mutation points the pre-flight may degrade
#: (:attr:`RuntimeConfig.preflight_probe_points`). Five is chosen to cover one
#: point per declared ``role`` on a realistic multi-agent harness (the
#: presentation target declares five: coordinator routing, system instruction,
#: tool description, path logic, topic naming), which is the sample the
#: round-robin selection in :func:`zicato.epoch.preflight.select_probe_points`
#: draws. It is a ceiling and not a cost: probing stops at the first point
#: whose signal clears both the noise floor and ``promote_margin``, so a
#: healthy contract spends exactly ONE degraded draw, exactly as it did before
#: issue #106 — the extra evidence is bought only where the alternative is
#: calling a contract unmeasurable on a sample of one.
PREFLIGHT_PROBE_POINTS_DEFAULT: int = 5

#: CEILING on :attr:`RuntimeConfig.preflight_probe_points`. Probe ``j`` draws at
#: ``PREFLIGHT_REPLICATE_BASE + j``, so a sample wider than the pre-flight's
#: reserved replicate block would squat the candidate screen's range (base 3000)
#: and make ITS cache idempotence a lie — the pre-flight already refuses such a
#: sample, but only after enumerating the snapshot, whereas a knob validated at
#: construction fails at the config that set it.
#:
#: Mirrors :data:`zicato.epoch.preflight.PREFLIGHT_REPLICATE_SPAN`, which owns
#: the fact; duplicated as a plain int rather than imported so :mod:`zicato.core`
#: keeps no dependency on :mod:`zicato.epoch` (and so validating a dataclass
#: field does not drag the pre-flight's import graph into every worker). The two
#: are pinned equal by
#: ``tests/test_preflight_severity_and_config_gate.py``.
PREFLIGHT_PROBE_POINTS_MAX: int = 1000


class RoundTokenLedger:
    """ONE round's mutable token accounting for ``max_tokens_per_round``.

    The orchestrator mints a fresh ledger per round (when the knob is on)
    and rebinds it onto the round's :class:`RuntimeConfig` via
    ``dataclasses.replace``, so every runner seam that already receives
    the config — the full/fast board-unit schedulers, the candidate
    screen, the evidence-gate replicate duels — shares one tally with no
    signature changes. Every FRESH board-unit run adds its
    ``LossProfile.tokens_spent`` (cache hits spend nothing and add
    nothing); the schedulers consult :meth:`check_and_clip` between board
    units / replicate slots and stop scheduling once the budget is spent.

    Token counts are OPPORTUNISTIC by the ``cost:`` namespace's contract
    (a harness without token-accounting middleware reports 0), so a
    ledger can only ever under-count — the budget is a best-effort
    guard, never a hard metering guarantee.

    Single-threaded by design: mutations happen on the orchestrator's
    event loop with no awaits between read and write.
    """

    __slots__ = ("max_tokens", "spent", "clipped")

    def __init__(self, max_tokens: int) -> None:
        self.max_tokens = int(max_tokens)
        self.spent = 0
        self.clipped = False

    def add(self, tokens: int) -> None:
        """Fold one fresh run's (non-negative) token spend into the tally."""
        self.spent += max(0, int(tokens))

    @property
    def exhausted(self) -> bool:
        """True once the tally has reached a positive budget."""
        return self.max_tokens > 0 and self.spent >= self.max_tokens

    def check_and_clip(self) -> bool:
        """Return :attr:`exhausted`, latching :attr:`clipped` when true.

        The schedulers call this at every would-launch point; the latched
        flag is how the orchestrator knows the round was token-clipped
        (the health finding) without threading a result back through the
        runner stack.
        """
        if self.exhausted:
            self.clipped = True
            return True
        return False


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    """The runtime-side parameters that bind one zicato instance.

    Fields
    ------
    instance_id:
        Identifier for this zicato instance. Distinguishes nested
        instances when an outer zicato is optimizing an inner zicato
        (the target-3 dogfood plan). v0 single-instance runs pass a
        constant (e.g. ``"default"``); future nested runs key
        workspaces, event streams, and lineage by this id.
    workspace_root:
        Absolute path to the ``.zicato/`` directory this instance
        writes under.
    harness_call_llm:
        LLM callable used BY the inner harness during runs. Zicato
        never invokes this directly; it is forwarded to the harness
        adapter at construction.
    auxiliary_call_llm:
        LLM callable used by every zicato-internal LLM consumer — the
        emulator, the proposer, the judge, the analysis pass. MUST be
        a distinct callable from :attr:`harness_call_llm` (identity-
        unequal) so the emulator cannot trivially collude with the
        inner harness through shared state.
    judge_call_llm:
        Optional LLM callable used by the in-run process judges /
        rubric matchers. ``None`` (the default) ⇒ judges fall back to
        :attr:`auxiliary_call_llm` (today's behavior). When set (from
        the workspace ``models.judge`` block) it lets an operator point
        the judges at a separate endpoint/model from the rest of the
        auxiliary surface. Read via :meth:`effective_judge_call_llm`.
    adjudicator_call_llm:
        Optional LLM callable used by the board-reflection meta-judge
        (the independent adjudicator that re-reads a captured transcript
        and decides whether each judge got it right — pillar 3). ``None``
        (the default) ⇒ the adjudicator falls back to
        :attr:`auxiliary_call_llm`, mirroring :attr:`judge_call_llm`'s
        fall-back onto the same surface. Read via
        :meth:`effective_adjudicator_call_llm`. Independence is
        load-bearing: the adjudication engine asserts this callable is
        identity-distinct from the judge callable before adjudicating
        (:func:`zicato.core.workspace.assert_distinct_callables`) — a
        judge cannot grade its own homework.
    proposer_breadth_call_llm:
        Optional LLM callable used by the best-of-N proposer's SLATE
        SAMPLING (WS-ENS ensemble roles — the "breadth" of AlphaEvolve's
        proposer ensemble). ``None`` (the default) ⇒ sampling falls back
        to :attr:`auxiliary_call_llm` (today's behavior, byte-identical),
        so an absent role changes nothing. When set (from a workspace
        ``models.proposer_breadth`` block) it points the exploratory
        slate samples at a separate endpoint/model — typically a cheaper,
        higher-temperature model that generates many diverse candidates.

        Live read path: the orchestrator threads this onto
        :class:`~zicato.proposer.best_of_n.BestOfNProposerAgent`, which
        swaps it onto ``ctx.aux_call_llm`` at the sampling site, FALLING
        BACK to the context's own ``ctx.aux_call_llm`` when ``None``. The
        default ADK proposer does not read ``ctx.aux_call_llm`` at all —
        the callable steers only proposers that DO (the text-shim / custom
        path); :attr:`proposer_breadth_model` carries the model-name string
        that makes the DEFAULT proposer honor a spec-configured role.

        NO collusion identity-guard applies between this and
        :attr:`proposer_depth_call_llm`: both are PROPOSER-SIDE roles in
        the SAME trust domain (the proposer stack, inside one
        overfitting-visibility envelope). The collusion guard exists only
        to keep an EVALUATOR distinct from the thing it evaluates (harness
        vs auxiliary; judge vs adjudicator) — breadth and depth are two
        halves of one proposer and may freely be the same callable.
    proposer_depth_call_llm:
        Optional LLM callable used by the best-of-N proposer's DEPTH
        passes — the self-CRITIQUE selection call and the screen-informed
        REVISE re-sample (and the future LLM-guided recombination merge).
        ``None`` (the default) ⇒ these fall back to
        :attr:`auxiliary_call_llm` (today's behavior, byte-identical).
        When set (from ``models.proposer_depth``) it points the
        refine/critique step at a separate endpoint/model — typically a
        stronger, lower-temperature model that judges + repairs the slate.

        Live read path: mirrors :attr:`proposer_breadth_call_llm` — the
        wrapper routes the critique call through this callable directly and
        swaps it onto ``ctx.aux_call_llm`` for the revise re-sample,
        falling back to the context's ``ctx.aux_call_llm`` when ``None``.
        :attr:`proposer_depth_model` carries the paired model string. See
        the no-collusion-guard note on :attr:`proposer_breadth_call_llm`.
    proposer_breadth_model:
        Optional MODEL-NAME string paired with
        :attr:`proposer_breadth_call_llm`: the resolved model name when the
        breadth role was configured via a ``models.proposer_breadth`` *model
        spec*. ``None`` when the role is absent OR was given as a bare
        ``call_llm`` dotted path (no model name) / injected as a raw
        callable (the test seam). The orchestrator threads it onto
        :class:`~zicato.proposer.best_of_n.BestOfNProposerAgent`, which
        swaps it onto ``ctx.model`` at the sampling site so the DEFAULT ADK
        proposer — which binds the model STRING, not ``ctx.aux_call_llm`` —
        reaches the role's endpoint. ``None`` ⇒ ``ctx.model`` keeps its own
        value (byte-identical).
    proposer_depth_model:
        Optional MODEL-NAME string paired with
        :attr:`proposer_depth_call_llm`, mirroring
        :attr:`proposer_breadth_model` for the DEPTH revise re-sample.
        (The critique call passes ``ctx.model`` straight to the depth
        callable, so no ``ctx.model`` swap is needed there.)
    seed:
        Optional integer seed for any zicato-internal random number
        generators. Adapters may or may not honor it for the inner
        harness.
    parallelism:
        Maximum number of **board units** the tournament runner keeps
        in flight at once — i.e. "how many boards run in parallel". The
        unit of scheduling is a board unit: one per board entry. In full
        mode a board unit runs its champion (parent) and challenger
        (child) runs CONCURRENTLY, so ``parallelism`` board units mean
        up to ``2 * parallelism`` run subprocesses alive at once; in
        fast mode a unit runs only the challenger, so up to
        ``parallelism`` subprocesses. ``1`` admits one board unit at a
        time (still two concurrent subprocesses per full-mode unit).
        Values above ``1`` let the runner play several "boards" of the
        tournament hall simultaneously, bounded by an
        :class:`asyncio.Semaphore`. The real-world ceiling is almost
        always the LLM endpoint's own concurrency limit, not this
        number — size it against ``2 * parallelism`` for full mode — so
        a modest default (``4``) is a safe starting point; operators
        raise it only when the endpoint can absorb more in-flight calls.
        Must be ``>= 1``.
    host_worker_permits:
        HOST-WIDE ceiling on board-unit worker subprocesses alive at once,
        across EVERY orchestrator on the machine. :attr:`parallelism` is a
        per-process :class:`asyncio.Semaphore` and therefore bounds only
        the run that owns it: two concurrent ``evolve`` runs on one box
        admit ``2 * parallelism`` board units between them (up to
        ``4 * parallelism`` workers in full mode), each resolving a
        ~246 MB import graph. This knob is the missing bound — a permit
        taken from a file-lock pool in the user's runtime directory
        (workspace-EXTERNAL, so the cap spans workspaces) before a worker
        is spawned and released once it is reaped. See
        :mod:`zicato.runtime.spawn_permit` and RUNTIME.md §5.5.7.

        ``None`` — the DEFAULT — means AUTO:
        ``max(4, 2 * os.cpu_count())``, deliberately generous enough that
        a single ordinary run never waits on a permit. ``0`` disables the
        cap entirely (no filesystem is touched). ``>= 1`` is an explicit
        ceiling. A run whose permits are all held QUEUES rather than
        over-subscribing; the throttle degrades OPEN on any
        infrastructure failure (no usable runtime dir, no ``flock``), so
        it can never be the reason a run fails to start.

        A RUNTIME tuning knob, NOT part of the frozen evaluation contract
        — it never enters the scoring canonical form, so changing it does
        not roll the epoch. Negative values are clamped to ``0`` (off)
        rather than rejected: a throttle must not fail a run on a typo.
    propose_parallelism:
        Maximum number of best-of-N slate SAMPLES the proposer keeps in
        flight at once — the propose-phase analogue of :attr:`parallelism`
        (which bounds board-unit runs). The N samples of a best-of-N slate
        are genuinely independent (each varies only by a deterministic
        per-slot hint), so the wrapper gathers them under an
        :class:`asyncio.Semaphore` sized from this value; the deterministic
        post-gather pass then emits every ``candidate_sampled`` event and
        appends every candidate in SLOT order, so the observable outcome is
        independent of completion order. ``1`` runs the slate fully serially
        and reproduces the pre-concurrency behaviour byte-for-byte. Default
        ``4``, mirroring :attr:`parallelism`; the real ceiling is almost
        always the LLM endpoint's own concurrency limit. A RUNTIME tuning
        knob, NOT part of the frozen evaluation contract — it never enters
        the scoring canonical form (it lives on :class:`RuntimeConfig`, which
        is never fed to the contract canonicalizer), so flipping it does not
        roll the epoch. Must be ``>= 1``.
    scrub_worker_env:
        When ``True``, each tournament worker is spawned with a MINIMAL
        explicit environment — the process-essential keys plus the
        ``api_key_env`` names the configured model roles need (and any
        :attr:`worker_env_passthrough` keys) — instead of inheriting the
        orchestrator's full environment. This denies a mutated worker
        read-access to every credential in the orchestrator's process env.
        Defaults to ``False`` (full inheritance — today's behavior), so a
        run is byte-for-byte unchanged unless an operator opts in.
    worker_env_passthrough:
        Extra environment-variable NAMES a scrubbed worker should still
        receive (a target that reads a bespoke variable). Only consulted
        when :attr:`scrub_worker_env` is ``True``; each name is copied from
        the orchestrator's env only if present. Empty by default.
    diversity_tolerance:
        Optional field-diversity overlap ceiling for the multi-challenger
        (non-gauntlet) path. ``None`` (the default) disables enforcement
        entirely, so a field of N challengers runs byte-for-byte as it does
        today (the only diversity guard is the pre-existing exact-duplicate
        soft-reject). When SET to a fraction in ``(0, 1]``, a challenger
        whose targeted-mutation-id set overlaps an already-accepted sibling's
        by a Jaccard ratio STRICTLY GREATER than this tolerance is
        *soft-rejected* — dropped from the run slate and recorded with a
        ``diversity_status`` of ``"soft_rejected"`` — so two challengers that
        touch essentially the same mutation points cannot collapse a field of
        N into fewer real experiments. A small value (e.g. ``0.5``) rejects
        heavily-overlapping siblings; ``1.0`` rejects nothing on this basis
        (no overlap can exceed 1.0), which is functionally equivalent to off.
        This is a RUNTIME tuning knob, NOT part of the frozen evaluation
        contract — flipping it does not roll the epoch. Must be in ``(0, 1]``
        when set.
    supervisor_kill_wait_s:
        Seconds the tournament parent waits for the SUPERVISOR to
        escalate-kill an over-budget worker after the parent writes the
        kill-request marker, BEFORE falling back to its own last-resort
        SIGTERM→grace→SIGKILL escalation. The supervisor is the single
        escalator: this window must comfortably exceed the supervisor's
        SIGTERM→SIGKILL grace plus its watchdog tick so a healthy
        supervisor always wins the kill. When NO supervisor is attached
        (an ad-hoc run with no watchdog, or a supervisor that itself
        died), this value is the ABORT-LATENCY FLOOR: every over-budget
        run waits the full window before the parent's fallback reaps the
        worker. The default (``20.0``) is generous on purpose — a few
        extra seconds on an already-overrun run is cheap; a leaked worker
        is not. Tests and supervisor-less harnesses shrink it to keep
        that floor from dominating wall-clock time.
    infra_abort_round_threshold:
        Endpoint-outage circuit breaker (WS-H). ``0`` (the DEFAULT) is
        OFF — an all-infra-aborted round settles exactly as today (the
        aborted runs score worst-case and the child is rejected). When
        ``>= 1``: after a gauntlet round's tournament settles, the
        orchestrator counts the duel's INFRA-aborted runs
        (:func:`zicato.core.loss.is_infra_abort_cause` — worker crashes,
        parent/supervisor kills; never a genuine budget exhaustion) and,
        at or above this threshold, the round DEFERS instead of burning
        the experiment: the tournament's verdict is discarded, nothing
        is journaled/finalized (the experiment persists un-outcomed, the
        exact shape the conservative crash-resume already reconciles),
        and the evolve loop backs off before the next round. A RUNTIME
        tuning knob, NOT part of the frozen evaluation contract —
        flipping it does not roll the epoch. Must be ``>= 0``.
    infra_backoff_base_s:
        First backoff delay (seconds) after a round defers on the infra
        circuit; consecutive deferrals double it. Only consulted while
        :attr:`infra_abort_round_threshold` is on. Must be ``>= 0``.
    infra_backoff_cap_s:
        Ceiling (seconds) on the exponential infra backoff. Must be
        ``>= 0``.
    preflight_gate:
        Contract pre-flight gate mode (issue #84). One of
        :data:`PREFLIGHT_GATE_MODES` — ``"off"`` | ``"warn"`` | ``"refuse"``.
        At evolve start (round 0, once per epoch, idempotent, best-effort)
        the loop measures the contract's A/A noise floor AND its degradation
        signal (champion vs a deliberately-degraded copy of itself; see
        :mod:`zicato.epoch.preflight`). ``"warn"`` (the DEFAULT) LOUDLY warns
        when the measured signal does not clear the noise floor (or the
        contract is saturated) and lets the run proceed — matching the
        recommend-only philosophy; ``"refuse"`` additionally HARD-STOPS the
        run (``PreflightRefusedError``) before rounds burn budget on a
        contract that cannot be optimized; ``"off"`` runs no pre-flight —
        UNLESS a legacy ``contract_preflight: K`` key is present (which was the
        pre-#84 opt-in, so ``"off"`` preserves that exact behaviour). With no
        such key — the common case, incl. deterministic oracles that assert
        their own known answer — ``"off"`` is byte-identical to pre-#84 (no
        measurement). A RUNTIME tuning knob, NOT part of the frozen evaluation
        contract — flipping it does not roll the epoch. The legacy
        ``config.json`` ``"contract_preflight": K`` key still sets the number
        of A/A draws K; absent, K defaults to ``DEFAULT_CALIBRATION_RUNS``.
        COST: under ``"warn"``/``"refuse"`` the once-per-epoch measurement runs
        ~K+1 champion board evaluations (the A/A draws + one degraded probe) at
        evolve start; it is idempotent (persisted; a resume re-reads the record)
        and skipped entirely on any infra abort (an outage never disqualifies a
        contract), but on a real endpoint it is real budget counted against
        round 0.
    preflight_probe_points:
        CEILING on how many mutation points the pre-flight may degrade to
        measure the degradation signal (issue #106). Defaults to
        :data:`PREFLIGHT_PROBE_POINTS_DEFAULT`; must be ``>= 1`` (``1``
        reproduces the single-probe behaviour that made one inert point able
        to veto a whole contract) and ``<=``
        :data:`PREFLIGHT_PROBE_POINTS_MAX` (the pre-flight's reserved
        replicate block cannot hold a wider sample). The pre-flight degrades
        a deterministic, role-diverse sample of this size
        (:func:`zicato.epoch.preflight.select_probe_points`) and reports the
        MAX signal, so one point that happens not to reach the deliverable can
        no longer produce a spurious ``refuse``. COST: a ceiling, not a spend
        — probing stops at the first point clearing both the noise floor and
        ``promote_margin``, so the healthy case is one degraded draw and the
        extra evaluations are paid only on a contract that looks unmeasurable.
        A RUNTIME tuning knob, NOT part of the frozen evaluation contract —
        changing it does not roll the epoch.
    preflight_probe_mutation_ids:
        Explicit pre-flight probe selection: the mutation-point ids to degrade,
        in order, INSTEAD of the automatic sample (``()`` — the default — means
        sample automatically). Use it when the operator knows which point
        carries the contract's signal, e.g. a coordinator instruction that
        every run exercises. Ignores :attr:`preflight_probe_points` (naming the
        points answers the selection question) and probes named points even
        when their degradation is a no-op, so a pin measures exactly what was
        asked. An id that does not enumerate under the champion snapshot fails
        the measurement loudly rather than silently falling back to the
        automatic sample, which would report a verdict measured on points the
        operator did not choose. ``zicato board preflight
        --degrade-mutation-id`` is the one-shot equivalent. A RUNTIME tuning
        knob, NOT part of the frozen evaluation contract.
    max_tokens_per_round:
        Per-round token budget (WS-H). ``0`` (the DEFAULT) is OFF —
        byte-identical scheduling. When ``>= 1``, the orchestrator mints
        a fresh :class:`RoundTokenLedger` per round; every fresh board
        unit run (parent + child + evidence replicates + candidate
        screen) folds its opportunistic ``cost:tokens_spent`` into the
        tally, and once it is spent the schedulers stop LAUNCHING
        further board units / replicate slots and the round settles with
        what it has (un-run units record the same budget-exceeded losses
        a matchup-deadline trip synthesizes; completed replicate slots
        average as-is). A RUNTIME tuning knob, NOT part of the frozen
        evaluation contract. Must be ``>= 0``.
    token_ledger:
        The ROUND-scoped mutable :class:`RoundTokenLedger`, rebound per
        round by the orchestrator when :attr:`max_tokens_per_round` is
        on (the ``inner_model`` live-object precedent). ``None`` — the
        default, and every round with the knob off — disables every
        ledger consultation. Never read from workspace config.
    persist_run_results:
        Persist each run's :class:`~zicato.core.RunResult` (the
        user-facing transcript + final output) as ``result.json`` beside
        the run's ``loss.json`` (replicate-slotted ``result.r{n}.json``,
        see :func:`zicato.tournament.unit_cache.unit_result_path`).
        DEFAULT ``True`` — always-on with an opt-out, because the
        artifact is small text and an opt-in would leave board
        reflection's passive tier permanently starved of verbatim
        transcripts (BOARD-REFLECTION.md's capture gap). The write is
        best-effort and atomic; a capture failure NEVER re-scores or
        aborts a run. A RUNTIME tuning knob, additive, NEVER part of the
        frozen evaluation contract (never hashed) — flipping it does not
        roll the epoch.
    persist_judge_io:
        Persist every inline judge ``evaluate`` call's verbatim I/O (the
        exact reasoning text judged + the raw LLM response + the parsed
        verdict) as an append-only ``judge_io.jsonl`` sidecar beside the
        run's ``loss.json`` (``judge_io.r{n}.jsonl`` per replicate; see
        :mod:`zicato.judge_runtime.io_capture`). DEFAULT ``True`` for
        the same always-on-with-opt-out rationale as
        :attr:`persist_run_results`; best-effort (a capture failure
        never changes a verdict or aborts a run). A RUNTIME tuning knob,
        additive, NEVER contract-hashed — flipping it does not roll the
        epoch.
    judge_io_sink:
        The LIVE judge-I/O sink object (the
        :class:`zicato.judge_runtime.io_capture.JudgeIOSink` protocol)
        the worker binds per run when :attr:`persist_judge_io` is on —
        the ``token_ledger`` / ``inner_model`` live-object precedent.
        ``None`` (the default, and every run with the knob off) disables
        capture entirely: the judge path is byte-identical to before the
        seam existed. Never read from workspace config. Typed ``Any`` so
        :mod:`zicato.core` carries no import dependency on the capture
        module.

    Construction-time validation
    ----------------------------
    The frozen dataclass does NOT validate the two-callable rule on
    construction (frozen dataclasses cannot run interesting
    ``__post_init__`` logic against the slotted fields without
    workarounds, and we keep this dataclass cheap to construct from
    JSON+factory paths in tests). Instead, call
    :func:`zicato.core.workspace.assert_distinct_callables` from the
    construction site before handing the :class:`RuntimeConfig` to the
    runner. The runner re-checks at startup as a defense in depth.

    The one check the dataclass DOES run in :meth:`__post_init__` is the
    cheap, scalar ``parallelism >= 1`` bound: an out-of-range value is a
    plain programming error (a sub-one semaphore is meaningless) caught
    far better at construction than deep inside the runner's gather. It
    reads no callable identity and mutates no field, so it does not
    reopen the deliberately-deferred two-callable validation above.
    """

    instance_id: str
    workspace_root: Path
    harness_call_llm: CallLLM
    auxiliary_call_llm: CallLLM
    seed: int | None = None
    parallelism: int = 4
    propose_parallelism: int = 4
    judge_call_llm: CallLLM | None = None
    adjudicator_call_llm: CallLLM | None = None
    user_emulator_call_llm: CallLLM | None = None
    proposer_breadth_call_llm: CallLLM | None = None
    proposer_depth_call_llm: CallLLM | None = None
    proposer_breadth_model: str | None = None
    proposer_depth_model: str | None = None
    scrub_worker_env: bool = False
    worker_env_passthrough: tuple[str, ...] = ()
    diversity_tolerance: float | None = None
    supervisor_kill_wait_s: float = 20.0
    infra_abort_round_threshold: int = 0
    infra_backoff_base_s: float = INFRA_BACKOFF_BASE_S_DEFAULT
    infra_backoff_cap_s: float = INFRA_BACKOFF_CAP_S_DEFAULT
    max_tokens_per_round: int = 0
    preflight_gate: str = PREFLIGHT_GATE_DEFAULT
    preflight_probe_points: int = PREFLIGHT_PROBE_POINTS_DEFAULT
    preflight_probe_mutation_ids: tuple[str, ...] = ()
    token_ledger: RoundTokenLedger | None = None
    #: The ADK model object (a ``BaseLlm``, typically a ``LiteLlm``) the inner
    #: ADK agents run on, built from a ``models.harness`` *model spec* (model +
    #: endpoint + api_key_env) via :func:`zicato.models_config.build_adk_model`.
    #: When set, the ADK adapter rebinds the target's string-model agents to it
    #: so they reach the configured endpoint with native tool/function calling
    #: intact — the config-driven alternative to a bare model string + the
    #: text-only ``call_llm`` shim. ``None`` (the default) ⇒ no inner model was
    #: configured; the adapter falls back to its guarded shim rebind. Typed
    #: ``Any`` so :mod:`zicato.core` carries no import dependency on ADK.
    inner_model: Any = None
    persist_run_results: bool = True
    persist_judge_io: bool = True
    judge_io_sink: Any = None
    #: HOST-WIDE ceiling on concurrently-alive worker subprocesses, across
    #: every orchestrator on the machine (:attr:`parallelism` bounds only
    #: this process). ``None`` — the default — is AUTO
    #: (``max(4, 2 * cores)``); ``0`` disables the cap; ``>= 1`` is an
    #: explicit ceiling. Declared LAST so the positional field order of
    #: every existing construction site is unchanged. See the class
    #: docstring and :mod:`zicato.runtime.spawn_permit`.
    host_worker_permits: int | None = None

    def __post_init__(self) -> None:
        """Validate the cheap scalar invariants (``parallelism`` + tolerance)."""
        if self.parallelism < 1:
            raise ValueError(
                f"RuntimeConfig.parallelism must be >= 1, got {self.parallelism!r}; "
                "use 1 for fully sequential board execution"
            )
        if self.propose_parallelism < 1:
            raise ValueError(
                f"RuntimeConfig.propose_parallelism must be >= 1, got "
                f"{self.propose_parallelism!r}; use 1 for a fully serial best-of-N slate"
            )
        if self.diversity_tolerance is not None and not (0.0 < self.diversity_tolerance <= 1.0):
            raise ValueError(
                "RuntimeConfig.diversity_tolerance must be in (0, 1] or None, "
                f"got {self.diversity_tolerance!r}; use None to disable "
                "field-diversity enforcement"
            )
        if self.infra_abort_round_threshold < 0:
            raise ValueError(
                "RuntimeConfig.infra_abort_round_threshold must be >= 0, got "
                f"{self.infra_abort_round_threshold!r}; use 0 to disable the "
                "endpoint-outage circuit"
            )
        if self.infra_backoff_base_s < 0 or self.infra_backoff_cap_s < 0:
            raise ValueError(
                "RuntimeConfig.infra_backoff_base_s / infra_backoff_cap_s must "
                f"be >= 0, got {self.infra_backoff_base_s!r} / "
                f"{self.infra_backoff_cap_s!r}"
            )
        if self.max_tokens_per_round < 0:
            raise ValueError(
                "RuntimeConfig.max_tokens_per_round must be >= 0, got "
                f"{self.max_tokens_per_round!r}; use 0 to disable the per-round "
                "token budget"
            )
        if self.preflight_gate not in PREFLIGHT_GATE_MODES:
            raise ValueError(
                f"RuntimeConfig.preflight_gate must be one of {PREFLIGHT_GATE_MODES}, "
                f"got {self.preflight_gate!r}"
            )
        if self.preflight_probe_points < 1:
            raise ValueError(
                "RuntimeConfig.preflight_probe_points must be >= 1, got "
                f"{self.preflight_probe_points!r}; use 1 to probe a single "
                "mutation point (the pre-#106 behaviour)"
            )
        if self.preflight_probe_points > PREFLIGHT_PROBE_POINTS_MAX:
            raise ValueError(
                "RuntimeConfig.preflight_probe_points must be <= "
                f"{PREFLIGHT_PROBE_POINTS_MAX} (the width of the pre-flight's "
                "reserved replicate block), got "
                f"{self.preflight_probe_points!r}; a wider sample would draw "
                "into the candidate screen's replicate range"
            )

    def effective_judge_call_llm(self) -> CallLLM:
        """The callable judges run on: :attr:`judge_call_llm` or the auxiliary.

        Judges historically run on :attr:`auxiliary_call_llm`; a workspace
        ``models.judge`` block may override them onto a separate endpoint via
        :attr:`judge_call_llm`. This single accessor centralises that
        fall-back so every judge call site reads the same rule.
        """
        return self.judge_call_llm if self.judge_call_llm is not None else self.auxiliary_call_llm

    def effective_user_emulator_call_llm(self) -> CallLLM:
        """The user-emulator callable, or the evaluation default."""
        return (
            self.user_emulator_call_llm
            if self.user_emulator_call_llm is not None
            else self.auxiliary_call_llm
        )

    def effective_adjudicator_call_llm(self) -> CallLLM:
        """The callable the reflection adjudicator runs on.

        :attr:`adjudicator_call_llm` when set, else the auxiliary surface
        — the same fall-back rule as :meth:`effective_judge_call_llm`.

        This fall-back exists only so a config is CONSTRUCTIBLE without a
        dedicated adjudicator callable; it is NOT a licence to adjudicate
        on the auxiliary endpoint. Active adjudication REQUIRES a callable
        distinct from every judge's: if the judges also run on the
        auxiliary surface (the common case), the auxiliary fall-back is the
        SAME object the judges use, and
        :func:`zicato.reflection.adjudicator.adjudicate_corpus` refuses via
        :func:`zicato.core.workspace.assert_distinct_callables` (a judge
        cannot grade its own homework). Configure a real adjudicator (a
        ``models`` block or ``--adjudicator-call-llm``) before adjudicating;
        this accessor only resolves the construction-time fall-back.
        """
        return (
            self.adjudicator_call_llm
            if self.adjudicator_call_llm is not None
            else self.auxiliary_call_llm
        )

    def effective_proposer_breadth_call_llm(self) -> CallLLM:
        """Convenience accessor mirroring :meth:`effective_judge_call_llm`.

        Returns :attr:`proposer_breadth_call_llm` when set, else the
        auxiliary surface. NOT the live read path: the best-of-N wrapper
        does its OWN fall-back onto the propose-time ``ctx.aux_call_llm``
        (the context, not this config, is the propose-time source of truth
        for the auxiliary surface), so it never calls this accessor. Kept
        for parity with the judge/adjudicator accessors and for callers that
        want the resolved callable off a config in hand.

        Unlike the judge/adjudicator accessors this carries NO distinctness
        obligation: breadth and depth are both proposer-side roles in one
        trust domain (see the field docstring), so the fall-back onto the
        shared auxiliary surface is not merely constructible but fully
        supported — it is the default.
        """
        return (
            self.proposer_breadth_call_llm
            if self.proposer_breadth_call_llm is not None
            else self.auxiliary_call_llm
        )

    def effective_proposer_depth_call_llm(self) -> CallLLM:
        """Convenience accessor mirroring :meth:`effective_proposer_breadth_call_llm`.

        Returns :attr:`proposer_depth_call_llm` when set, else the auxiliary
        surface. NOT the live read path (the wrapper falls back to the
        propose-time ``ctx.aux_call_llm`` itself); see the note on
        :meth:`effective_proposer_breadth_call_llm`. No distinctness
        obligation applies against the breadth role (same proposer-side
        trust domain); both defaulting to the auxiliary callable is the
        supported, byte-identical default.
        """
        return (
            self.proposer_depth_call_llm
            if self.proposer_depth_call_llm is not None
            else self.auxiliary_call_llm
        )
