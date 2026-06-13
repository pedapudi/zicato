"""Runtime-config types: the model-agnostic LLM shape + the runtime binding.

Split out of :mod:`zicato.core.types`; re-exported from there and from
:mod:`zicato.core` so existing import paths keep working.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

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

    def __post_init__(self) -> None:
        """Validate the cheap scalar invariants (currently ``parallelism``)."""
        if self.parallelism < 1:
            raise ValueError(
                f"RuntimeConfig.parallelism must be >= 1, got {self.parallelism!r}; "
                "use 1 for fully sequential board execution"
            )

    def effective_judge_call_llm(self) -> CallLLM:
        """The callable judges run on: :attr:`judge_call_llm` or the auxiliary.

        Judges historically run on :attr:`auxiliary_call_llm`; a workspace
        ``models.judge`` block may override them onto a separate endpoint via
        :attr:`judge_call_llm`. This single accessor centralises that
        fall-back so every judge call site reads the same rule.
        """
        return self.judge_call_llm if self.judge_call_llm is not None else self.auxiliary_call_llm
