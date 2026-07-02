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

    def effective_judge_call_llm(self) -> CallLLM:
        """The callable judges run on: :attr:`judge_call_llm` or the auxiliary.

        Judges historically run on :attr:`auxiliary_call_llm`; a workspace
        ``models.judge`` block may override them onto a separate endpoint via
        :attr:`judge_call_llm`. This single accessor centralises that
        fall-back so every judge call site reads the same rule.
        """
        return self.judge_call_llm if self.judge_call_llm is not None else self.auxiliary_call_llm
