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
        Achievable-signal pre-flight gate mode (issue #84). One of
        :data:`PREFLIGHT_GATE_MODES` — ``"off"`` | ``"warn"`` | ``"refuse"``.
        At evolve start (round 0, once per epoch, idempotent, best-effort)
        the loop measures the contract's A/A noise floor AND its achievable
        signal (champion vs a deliberately-degraded copy of itself; see
        :mod:`zicato.epoch.preflight`). ``"warn"`` (the DEFAULT) LOUDLY warns
        when the achievable signal does not clear the noise floor (or the
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
    judge_call_llm: CallLLM | None = None
    scrub_worker_env: bool = False
    worker_env_passthrough: tuple[str, ...] = ()
    diversity_tolerance: float | None = None
    supervisor_kill_wait_s: float = 20.0
    infra_abort_round_threshold: int = 0
    infra_backoff_base_s: float = INFRA_BACKOFF_BASE_S_DEFAULT
    infra_backoff_cap_s: float = INFRA_BACKOFF_CAP_S_DEFAULT
    max_tokens_per_round: int = 0
    preflight_gate: str = PREFLIGHT_GATE_DEFAULT
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

    def __post_init__(self) -> None:
        """Validate the cheap scalar invariants (``parallelism`` + tolerance)."""
        if self.parallelism < 1:
            raise ValueError(
                f"RuntimeConfig.parallelism must be >= 1, got {self.parallelism!r}; "
                "use 1 for fully sequential board execution"
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

    def effective_judge_call_llm(self) -> CallLLM:
        """The callable judges run on: :attr:`judge_call_llm` or the auxiliary.

        Judges historically run on :attr:`auxiliary_call_llm`; a workspace
        ``models.judge`` block may override them onto a separate endpoint via
        :attr:`judge_call_llm`. This single accessor centralises that
        fall-back so every judge call site reads the same rule.
        """
        return self.judge_call_llm if self.judge_call_llm is not None else self.auxiliary_call_llm
