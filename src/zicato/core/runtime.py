"""Runtime-config types: the model-agnostic LLM shape + the runtime binding.

Split out of :mod:`zicato.core.types`; re-exported from there and from
:mod:`zicato.core` so existing import paths keep working.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zicato.core.adapter_config import DriverImportContext
from zicato.core.run_context import RunContext
from zicato.core.runtime_context import TelemetryEndpoints
from zicato.core.settings import (
    INFRA_BACKOFF_BASE_S_DEFAULT as INFRA_BACKOFF_BASE_S_DEFAULT,
)
from zicato.core.settings import (
    INFRA_BACKOFF_CAP_S_DEFAULT as INFRA_BACKOFF_CAP_S_DEFAULT,
)
from zicato.core.settings import (
    PREFLIGHT_GATE_DEFAULT as PREFLIGHT_GATE_DEFAULT,
)
from zicato.core.settings import (
    PREFLIGHT_GATE_MODES as PREFLIGHT_GATE_MODES,
)
from zicato.core.settings import (
    PREFLIGHT_PROBE_POINTS_DEFAULT as PREFLIGHT_PROBE_POINTS_DEFAULT,
)
from zicato.core.settings import (
    ResolvedConfiguration,
    RuntimeSettings,
    resolve_configuration,
)

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
class RuntimeConfig(RuntimeSettings):
    """The runtime-side parameters that bind one zicato instance.

    Operational field descriptions belong to RuntimeSettings.

    Fields
    ------
    workspace_root:
        Absolute path to the ``.zicato/`` directory this instance
        writes under.
    target_call_llm:
        LLM callable used BY the system under test during runs. Zicato
        never invokes this directly; it is forwarded to the harness
        adapter at construction.
    evaluation_call_llm:
        LLM callable used by every zicato-internal LLM consumer — the
        emulator, the proposer, the judge, the analysis pass. MUST be
        a distinct callable from :attr:`target_call_llm` (identity-
        unequal) so the emulator cannot trivially collude with the
        system under test through shared state.
    judge_call_llm:
        Optional LLM callable used by the in-run process judges /
        rubric matchers. ``None`` (the default) ⇒ judges fall back to
        :attr:`evaluation_call_llm` (the default behavior). When set (from
        the workspace ``models.judge`` block) it lets an operator point
        the judges at a separate endpoint/model from the rest of the
        evaluation surface. Read via :meth:`effective_judge_call_llm`.
    adjudicator_call_llm:
        Optional LLM callable used by the board-reflection meta-judge
        (the independent adjudicator that re-reads a captured transcript
        and decides whether each judge got it right — pillar 3). ``None``
        (the default) ⇒ the adjudicator falls back to
        :attr:`evaluation_call_llm`, mirroring :attr:`judge_call_llm`'s
        fall-back onto the same surface. Read via
        :meth:`effective_adjudicator_call_llm`. Independence is
        load-bearing: the adjudication engine asserts this callable is
        identity-distinct from the judge callable before adjudicating
        (:func:`zicato.core.workspace.assert_distinct_callables`) — a
        judge cannot grade its own homework.
    proposer_breadth_call_llm:
        Optional callable for `proposer_generate`; falls back to the base
        proposer, then evaluation. It may equal the review callable.
    proposer_depth_call_llm:
        Optional callable for `proposer_review`, with the same inheritance.
    proposer_breadth_model:
        Model id paired with generate for native and process-backed proposers.
    token_ledger:
        The ROUND-scoped mutable :class:`RoundTokenLedger`, rebound per
        round by the orchestrator when :attr:`max_tokens_per_round` is
        on (the ``target_model`` live-object precedent). ``None`` — the
        default, and every round with the knob off — disables every
        ledger consultation. Never read from workspace config.
    judge_io_sink:
        The LIVE judge-I/O sink object (the
        :class:`zicato.judge_runtime.io_capture.JudgeIOSink` protocol)
        the worker binds per run when :attr:`persist_judge_io` is on —
        the ``token_ledger`` / ``target_model`` live-object precedent.
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
    reopen the deferred two-callable validation above.
    """

    workspace_root: Path
    target_call_llm: CallLLM
    evaluation_call_llm: CallLLM
    driver_imports: DriverImportContext = DriverImportContext()
    run_context: RunContext | None = None
    judge_call_llm: CallLLM | None = None
    adjudicator_call_llm: CallLLM | None = None
    user_emulator_call_llm: CallLLM | None = None
    proposer_call_llm: CallLLM | None = None
    proposer_breadth_call_llm: CallLLM | None = None
    proposer_depth_call_llm: CallLLM | None = None
    proposer_breadth_model: str | None = None
    proposer_depth_model: str | None = None
    proposer_model: str | None = None
    token_ledger: RoundTokenLedger | None = None
    #: The ADK model object (a ``BaseLlm``, typically a ``LiteLlm``) the inner
    #: ADK agents run on, built from a ``models.target`` *model spec* (model +
    #: endpoint + api_key_env) via :func:`zicato.models_config.build_adk_model`.
    #: When set, the ADK adapter rebinds the target's string-model agents to it
    #: so they reach the configured endpoint with native tool/function calling
    #: intact — the config-driven alternative to a bare model string + the
    #: text-only ``call_llm`` shim. ``None`` (the default) ⇒ no inner model was
    #: configured; the adapter falls back to its guarded shim rebind. Typed
    #: ``Any`` so :mod:`zicato.core` carries no import dependency on ADK.
    target_model: Any = None
    judge_io_sink: Any = None
    #: Goldfive measurement, steering, endpoint, and agent-limit settings from
    #: the epoch's frozen scoring contract. Tournament workers bind this field
    #: from the same :class:`ScoringWeights` instance they use to reduce loss.
    goldfive: Mapping[str, Any] | None = None
    configuration: ResolvedConfiguration | None = None
    # Immutable worker-role documents retained from the selected execution contract.
    execution_roles: bytes | None = None
    telemetry: TelemetryEndpoints = TelemetryEndpoints()

    def operational_configuration(self) -> ResolvedConfiguration:
        """Return selected settings, including explicit runtime adjustments."""
        from dataclasses import fields, replace  # noqa: PLC0415

        resolved = self.configuration or resolve_configuration({})
        updates = {
            item.name: getattr(self, item.name)
            for item in fields(RuntimeSettings)
            if getattr(self, item.name) != getattr(resolved.values.runtime, item.name)
        }
        if not updates:
            return resolved
        values = replace(resolved.values, runtime=replace(resolved.values.runtime, **updates))
        sources = {**resolved.sources, **{f"runtime.{key}": "invocation" for key in updates}}
        return ResolvedConfiguration(values, sources)

    def effective_judge_call_llm(self) -> CallLLM:
        """The callable judges run on: :attr:`judge_call_llm` or the evaluation callable.

        Judges run on :attr:`evaluation_call_llm` by default; a workspace
        ``models.judge`` block may override them onto a separate endpoint via
        :attr:`judge_call_llm`. This single accessor centralises that
        fall-back so every judge call site reads the same rule.
        """
        return self.judge_call_llm if self.judge_call_llm is not None else self.evaluation_call_llm

    def effective_user_emulator_call_llm(self) -> CallLLM:
        """The user-emulator callable, or the evaluation default."""
        return (
            self.user_emulator_call_llm
            if self.user_emulator_call_llm is not None
            else self.evaluation_call_llm
        )

    def effective_proposer_call_llm(self) -> CallLLM:
        """The base proposer callable, or the evaluation default."""
        return (
            self.proposer_call_llm
            if self.proposer_call_llm is not None
            else self.evaluation_call_llm
        )

    def effective_adjudicator_call_llm(self) -> CallLLM:
        """The callable the reflection adjudicator runs on.

        :attr:`adjudicator_call_llm` when set, else the evaluation surface
        — the same fall-back rule as :meth:`effective_judge_call_llm`.

        This fall-back exists only so a config is CONSTRUCTIBLE without a
        dedicated adjudicator callable; it is NOT a licence to adjudicate
        on the evaluation endpoint. Active adjudication REQUIRES a callable
        distinct from every judge's: if the judges also run on the
        evaluation surface (the common case), the evaluation fall-back is the
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
            else self.evaluation_call_llm
        )

    def effective_proposer_breadth_call_llm(self) -> CallLLM:
        """Convenience accessor mirroring :meth:`effective_judge_call_llm`.

        Returns :attr:`proposer_breadth_call_llm` when set, else the
        evaluation surface. NOT the live read path: the best-of-N wrapper
        does its OWN fall-back onto the propose-time ``ctx.aux_call_llm``
        (the context rather than this config is the propose-time source of truth
        for the evaluation surface), so it never calls this accessor. Kept
        for parity with the judge/adjudicator accessors and for callers that
        want the resolved callable off a config in hand.

        Unlike the judge/adjudicator accessors this carries NO distinctness
        obligation: breadth and depth are both proposer-side roles in one
        trust domain (see the field docstring), so the fall-back onto the
        shared evaluation surface is not merely constructible but fully
        supported — it is the default.
        """
        return (
            self.proposer_breadth_call_llm
            if self.proposer_breadth_call_llm is not None
            else self.evaluation_call_llm
        )

    def effective_proposer_depth_call_llm(self) -> CallLLM:
        """Convenience accessor mirroring :meth:`effective_proposer_breadth_call_llm`.

        Returns :attr:`proposer_depth_call_llm` when set, else the evaluation
        surface. NOT the live read path (the wrapper falls back to the
        propose-time ``ctx.aux_call_llm`` itself); see the note on
        :meth:`effective_proposer_breadth_call_llm`. No distinctness
        obligation applies against the breadth role (same proposer-side
        trust domain); both defaulting to the evaluation callable is the
        supported, byte-identical default.
        """
        return (
            self.proposer_depth_call_llm
            if self.proposer_depth_call_llm is not None
            else self.evaluation_call_llm
        )
