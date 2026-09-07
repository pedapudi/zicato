"""Immutable operational settings shared by invocation and runtime construction."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from types import MappingProxyType
from typing import Any

from zicato.core.configuration import (
    ConfigurationError,
    authored_dataclass_from_json,
    dataclass_to_jsonable,
    validate_authored_overlay,
)
from zicato.core.constraints import KnobConstraint, validate_knobs
from zicato.core.measurement import PREFLIGHT_REPLICATE_SPAN

#: Initial delay and maximum delay after an infrastructure failure, in seconds.
INFRA_BACKOFF_BASE_S_DEFAULT: float = 30.0
INFRA_BACKOFF_CAP_S_DEFAULT: float = 480.0

#: Preflight can be disabled, report findings, or refuse an unmeasurable contract.
PREFLIGHT_GATE_MODES: tuple[str, ...] = ("off", "warn", "refuse")
PREFLIGHT_GATE_DEFAULT: str = "warn"
PREFLIGHT_PROBE_POINTS_DEFAULT: int = 5

#: A probe must fit inside the measurement owner's reserved preflight interval.
PREFLIGHT_PROBE_POINTS_MAX: int = PREFLIGHT_REPLICATE_SPAN


@dataclass(frozen=True, slots=True)
class HealthConfig:
    """Thresholds for evolve-loop health detectors.

    Authored values must satisfy their declared types and bounds. A critical
    generalization gap cannot be below its warning threshold.

    Fields
    ------
    scoring_window:
        Number of most-recent tournaments
        :func:`~zicato.health.diagnostics.detect_degenerate_scoring`
        inspects. The detector fires only when *all* tournaments in the
        window are flat. Must be ``>= 1``.
    scoring_epsilon:
        Absolute ``scalar_score_delta`` below which a tournament counts
        as having produced no optimization signal. Must be ``>= 0``.
    no_expectations_fraction:
        Fraction-of-board-entries-without-an-expectation threshold for
        :func:`~zicato.health.diagnostics.detect_no_expectations`. The
        detector fires when the fraction is strictly greater than this.
        A fraction in ``[0, 1]``. The pre-spend workspace gate raises its
        board-coverage advisory at the same threshold — both read it
        through
        :func:`~zicato.board.expectation_coverage.measure_expectation_coverage`.
    stalled_rejects:
        Number of consecutive ``rejected`` generations
        :func:`~zicato.health.diagnostics.detect_stalled_loop` treats as
        a stall. Must be ``>= 1``.
    generalization_gap_warn:
        The generalization gap (``holdout_loss - train_loss``) at or above
        which
        :func:`~zicato.health.diagnostics.detect_generalization_gap`
        fires a ``warning`` — the champion's holdout is starting to lag its
        train slice (board memorization; OVERFITTING.md §6 / §12 #5). Must
        be ``>= 0``.
    generalization_gap_crit:
        The gap at or above which the detector fires ``critical`` and
        surfaces a board-refresh recommendation. Must be ``>= 0`` and is
        at least ``generalization_gap_warn``.
    """

    scoring_window: int = field(default=3, metadata={"constraint": KnobConstraint(minimum=1)})
    scoring_epsilon: float = field(default=1e-6, metadata={"constraint": KnobConstraint(minimum=0)})
    no_expectations_fraction: float = field(
        default=0.5, metadata={"constraint": KnobConstraint(minimum=0, maximum=1)}
    )
    stalled_rejects: int = field(default=3, metadata={"constraint": KnobConstraint(minimum=1)})
    generalization_gap_warn: float = field(
        default=0.05, metadata={"constraint": KnobConstraint(minimum=0)}
    )
    generalization_gap_crit: float = field(
        default=0.15, metadata={"constraint": KnobConstraint(minimum=0)}
    )

    def __post_init__(self) -> None:
        validate_knobs(self)
        if self.generalization_gap_crit < self.generalization_gap_warn:
            raise ConfigurationError(
                "health.generalization_gap_crit",
                "range",
                "must be at least generalization_gap_warn",
            )


@dataclass(frozen=True, slots=True)
class AuxConfig:
    """Configuration for evaluation-LLM calls (proposer / judge / analysis).

    Fields
    ------
    call_timeout_s:
        Per-call wall-clock budget, in seconds, for every evaluation-LLM
        invocation. A hung evaluation endpoint can wedge a round; each
        call site wraps its ``aux_call_llm`` invocation in
        :func:`asyncio.wait_for` against this budget. Operators tune it
        with ``zicato evolve --aux-call-timeout``. A non-positive value
        is meaningless — it would short-circuit every call — and the
        flag rejects it up front.
    """

    call_timeout_s: float = field(
        default=120.0,
        metadata={
            "constraint": KnobConstraint(minimum=0, exclusive_minimum=True),
            "cli": "--aux-call-timeout",
        },
    )

    def __post_init__(self) -> None:
        validate_knobs(self)


@dataclass(frozen=True, slots=True)
class IntegrationConfig:
    """Authored service locations and process-supervisor selection.

    Fields
    ------
    harmonograf_url:
        Browser URL of an external telemetry service. Empty selects a workspace
        service at invocation startup. Its native address is a runtime dependency.
    supervisor_binary:
        Path of the process watchdog executable. Empty uses the packaged or
        locally available supervisor according to the launch owner's policy.
    """

    harmonograf_url: str = field(default="", metadata={"cli": "--harmonograf-url"})
    supervisor_binary: str = field(default="", metadata={"cli": "--supervisor-binary"})


@dataclass(frozen=True, slots=True)
class DashboardConfig:
    """Configuration for the dashboard HTTP service.

    Fields
    ------
    static_dir:
        Filesystem path to the bundled dashboard static-asset directory,
        or empty string to fall back to the in-tree
        ``zicato/dashboard/static`` directory. Operators set it with
        ``zicato dashboard --static-dir`` / ``zicato dashboard --view builder
        --static-dir``. Useful for installed wheels that relocate the
        bundle and for tests.
    """

    static_dir: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeSettings:
    """Operational values carried unchanged from configuration to each runtime.

    Fields
    ------
    instance_id:
        Identifier for this zicato instance. Distinguishes nested
        instances when an outer zicato is optimizing an inner zicato
        (the target-3 dogfood plan). v0 single-instance runs pass a
        constant (e.g. ``"default"``); future nested runs key
        workspaces, event streams, and lineage by this id.
    seed:
        Optional integer seed for any zicato-internal random number
        generators. Adapters may or may not honor it for the system
        under test.
    parallelism:
        Maximum number of **board units** the tournament runner keeps
        in flight at once — i.e. "how many boards run in parallel". The
        unit of scheduling is a board unit: one per board entry. In full
        mode a board unit runs its champion (parent) and challenger
        (child) runs CONCURRENTLY, so ``parallelism`` board units mean
        up to ``2 * parallelism`` run subprocesses alive at once. Fast mode
        resolves both competitors from the replicate-keyed cache and runs
        only misses, so its active count ranges from zero to the same ceiling.
        ``1`` admits one board unit at a time.
        Values above ``1`` let the runner play several "boards" of the
        tournament hall simultaneously, bounded by an
        :class:`asyncio.Semaphore`. The real-world ceiling is almost
        always the LLM endpoint's own concurrency limit rather than this
        number — size it against ``2 * parallelism`` — so
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

        ``None`` and ``true`` select an automatic ceiling:
        ``max(4, 2 * os.cpu_count())``, generous enough that
        a single ordinary run never waits on a permit. ``0`` disables the
        cap entirely (no filesystem is touched), as does ``false``. ``>= 1`` is an explicit
        ceiling. A run whose permits are all held QUEUES rather than
        over-subscribing; the throttle degrades OPEN on any
        infrastructure failure (no usable runtime dir, no ``flock``), so
        it can never be the reason a run fails to start.

        A RUNTIME tuning knob, NOT part of the frozen evaluation contract
        — it never enters the scoring canonical form, so changing it does
        not roll the epoch. Negative values are rejected before execution.
    worker_permit_dir:
        Optional absolute, workspace-external directory holding the host-wide
        worker permit slots. ``None`` uses the platform runtime directory.
        An absolute path ensures orchestrators launched from different working
        directories share one permit pool. Configure this only when several
        orchestrators must share a nonstandard runtime filesystem.
    log_level:
        Minimum structured-log severity captured for orchestrator and worker
        records. One of ``DEBUG``, ``INFO``, ``WARNING``, ``ERROR``, or
        ``CRITICAL``, ignoring case. The default is ``INFO``.
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
        Defaults to ``False`` (full inheritance).
    worker_env_passthrough:
        Extra environment-variable NAMES a scrubbed worker should still
        receive (a target that reads a bespoke variable). Only consulted
        when :attr:`scrub_worker_env` is ``True``; each name is copied from
        the orchestrator's env only if present. Empty by default.
    diversity_tolerance:
        Optional field-diversity overlap ceiling for the multi-challenger
        (non-gauntlet) path. ``None`` (the default) disables enforcement
        entirely, leaving the exact-duplicate soft-reject as the only
        diversity guard. When SET to a fraction in ``(0, 1]``, a challenger
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
        Endpoint-outage circuit breaker. ``0`` (the DEFAULT) is OFF, and
        an all-infra-aborted round then settles like any other: the
        aborted runs score worst-case and the child is rejected. When
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
        signal (champion vs a degraded copy of itself; see
        :mod:`zicato.epoch.preflight`). ``"warn"`` (the DEFAULT) LOUDLY warns
        when the measured signal does not clear the noise floor (or the
        contract is saturated) and lets the run proceed — matching the
        recommend-only philosophy; ``"refuse"`` additionally HARD-STOPS the
        run (``PreflightRefusedError``) before rounds burn budget on a
        contract that cannot be optimized; ``"off"`` runs no pre-flight —
        UNLESS a ``contract_preflight: K`` key is present, which requests one
        explicitly. With no such key — the common case, including
        deterministic oracles that assert their own known answer — ``"off"``
        measures nothing at all. A RUNTIME tuning knob that is no part of the
        frozen evaluation contract, so flipping it does not roll the epoch.
        The ``config.json`` ``"contract_preflight": K`` key sets the number
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
        MAX signal, so one point that happens not to reach the deliverable
        cannot produce a spurious ``refuse``. COST: this is a ceiling rather
        than a spend — probing stops at the first point clearing both the
        noise floor and ``promote_margin``, so the healthy case is one
        degraded draw and the extra evaluations are paid only on a contract
        that looks unmeasurable.
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
        Per-round token budget. ``0`` (the DEFAULT) is OFF and leaves
        scheduling untouched. When ``>= 1``, the orchestrator mints
        a fresh :class:`RoundTokenLedger` per round; every fresh board
        unit run (parent + child + evidence replicates + candidate
        screen) folds its opportunistic ``cost:tokens_spent`` into the
        tally, and once it is spent the schedulers stop LAUNCHING
        further board units / replicate slots and the round settles with
        what it has (un-run units record the same budget-exceeded losses
        a matchup-deadline trip synthesizes; completed replicate slots
        average as-is). A RUNTIME tuning knob, NOT part of the frozen
        evaluation contract. Must be ``>= 0``.
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
    """

    instance_id: str = "default"
    seed: int | None = field(
        default=None,
        metadata={
            "scope": "evaluation-contract",
            "rolls_epoch": False,
            "description": (
                "Base seed for measured runs. It is frozen per invocation and distinguishes "
                "reusable measurement evidence; changing it does not change epoch hashes."
            ),
        },
    )
    parallelism: int = field(
        default=4,
        metadata={
            "constraint": KnobConstraint(minimum=1),
            "cli": "--parallelism",
            "null_uses_default": True,
        },
    )
    propose_parallelism: int = field(
        default=4, metadata={"constraint": KnobConstraint(minimum=1), "null_uses_default": True}
    )
    scrub_worker_env: bool = False
    worker_env_passthrough: tuple[str, ...] = field(
        default=(), metadata={"null_uses_default": True}
    )
    diversity_tolerance: float | None = field(
        default=None,
        metadata={
            "constraint": KnobConstraint(
                minimum=0, maximum=1, exclusive_minimum=True, allow_none=True
            )
        },
    )
    supervisor_kill_wait_s: float = field(
        default=20.0, metadata={"constraint": KnobConstraint(minimum=0)}
    )
    infra_abort_round_threshold: int = field(
        default=0, metadata={"constraint": KnobConstraint(minimum=0), "null_uses_default": True}
    )
    infra_backoff_base_s: float = field(
        default=INFRA_BACKOFF_BASE_S_DEFAULT,
        metadata={"constraint": KnobConstraint(minimum=0), "null_uses_default": True},
    )
    infra_backoff_cap_s: float = field(
        default=INFRA_BACKOFF_CAP_S_DEFAULT,
        metadata={"constraint": KnobConstraint(minimum=0), "null_uses_default": True},
    )
    max_tokens_per_round: int = field(
        default=0, metadata={"constraint": KnobConstraint(minimum=0), "null_uses_default": True}
    )
    preflight_gate: str = field(
        default=PREFLIGHT_GATE_DEFAULT,
        metadata={"constraint": KnobConstraint(choices=PREFLIGHT_GATE_MODES)},
    )
    preflight_probe_points: int = field(
        default=PREFLIGHT_PROBE_POINTS_DEFAULT,
        metadata={
            "constraint": KnobConstraint(minimum=1, maximum=PREFLIGHT_PROBE_POINTS_MAX),
            "null_uses_default": True,
        },
    )
    preflight_probe_mutation_ids: tuple[str, ...] = field(
        default=(), metadata={"null_uses_default": True}
    )
    persist_run_results: bool = True
    persist_judge_io: bool = True
    host_worker_permits: int | bool | None = field(
        default=None,
        metadata={"constraint": KnobConstraint(minimum=0, allow_none=True, allow_bool=True)},
    )
    worker_permit_dir: Path | None = None
    log_level: str = field(
        default="INFO",
        metadata={
            "constraint": KnobConstraint(choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")),
            "case_insensitive": True,
        },
    )

    def __post_init__(self) -> None:
        validate_knobs(self)
        if self.worker_permit_dir is not None and not self.worker_permit_dir.is_absolute():
            raise ConfigurationError(
                "runtime.worker_permit_dir", "value", "expected an absolute path"
            )
        if type(self.host_worker_permits) is bool:
            object.__setattr__(self, "host_worker_permits", None if self.host_worker_permits else 0)


@dataclass(frozen=True, slots=True)
class RuntimeDeclaration(RuntimeSettings):
    """Persisted runtime settings and importable role declarations.

    Fields
    ------
    workspace_root:
        Workspace path used by direct runtime construction when no path is supplied.
    target_call_llm:
        Importable target callable used when no named target engine is configured.
    evaluation_call_llm:
        Importable evaluation callable used when no named evaluation engine is configured.
    evaluation_model:
        Model name passed to evaluation calls.
    proposer_agent:
        Importable operator-supplied proposer class.
    """

    workspace_root: str = ".zicato"
    target_call_llm: str | None = None
    evaluation_call_llm: str | None = None
    evaluation_model: str = ""
    proposer_agent: str = ""


@dataclass(frozen=True, slots=True)
class ZicatoConfig:
    """Configuration composed from the owning operational records.

    Fields
    ------
    health:
        Thresholds for reporting stalled, flat, or overfit optimization loops.
    aux:
        Wall-clock budget for auxiliary evaluation calls.
    integration:
        External telemetry and process-supervisor locations.
    dashboard:
        Dashboard asset location used by HTTP service construction.
    runtime:
        Concurrency, measurement seed, worker containment, and runtime controls.
    """

    health: HealthConfig = HealthConfig()
    aux: AuxConfig = AuxConfig()
    integration: IntegrationConfig = IntegrationConfig()
    dashboard: DashboardConfig = DashboardConfig()
    runtime: RuntimeSettings = RuntimeSettings()


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    return value


def _merge(base: Mapping[str, Any], overrides: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overrides.items():
        existing = merged.get(key)
        merged[key] = (
            _merge(existing, value)
            if isinstance(existing, Mapping) and isinstance(value, Mapping)
            else value
        )
    return merged


def _leaves(raw: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    result = {}
    for key, value in raw.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, Mapping):
            result.update(_leaves(value, path))
        else:
            result[path] = value
    return result


@dataclass(frozen=True, slots=True)
class InvocationOverlay:
    """A detached immutable set of explicit settings for one invocation."""

    overrides: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        validate_authored_overlay(ZicatoConfig, self.overrides, path="config")
        object.__setattr__(self, "overrides", _freeze(self.overrides))

    @classmethod
    def from_mapping(cls, overrides: Mapping[str, Any]) -> InvocationOverlay:
        return cls(overrides)


@dataclass(frozen=True, slots=True)
class ResolvedConfiguration:
    """The operational values and declared sources selected for one invocation."""

    values: ZicatoConfig
    sources: Mapping[str, str]

    def __post_init__(self) -> None:
        expected = _leaves(dataclass_to_jsonable(self.values))
        if set(self.sources) != set(expected):
            raise ConfigurationError(
                "configuration.sources", "value", "sources must cover every configured field"
            )
        if any(
            type(source) is not str or source not in {"default", "workspace", "invocation"}
            for source in self.sources.values()
        ):
            raise ConfigurationError(
                "configuration.sources", "value", "unknown configuration source"
            )
        object.__setattr__(self, "sources", MappingProxyType(dict(self.sources)))

    def to_json(self) -> dict[str, Any]:
        return {"values": dataclass_to_jsonable(self.values), "sources": dict(self.sources)}

    def effective_settings(self) -> dict[str, dict[str, Any]]:
        """List each persisted field with its value and selected source."""
        return {
            path: {"value": value, "source": self.sources[path]}
            for path, value in _leaves(dataclass_to_jsonable(self.values)).items()
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> ResolvedConfiguration:
        if not isinstance(payload, Mapping) or set(payload) != {"values", "sources"}:
            raise ConfigurationError("configuration", "value", "expected values and sources")
        values = authored_dataclass_from_json(
            ZicatoConfig, payload["values"], path="configuration.values"
        )
        sources = payload["sources"]
        if not isinstance(sources, Mapping):
            raise ConfigurationError("configuration.sources", "type", "expected an object")
        return cls(values, sources)


def resolve_configuration(
    workspace_config: Mapping[str, Any], *, overlay: InvocationOverlay | None = None
) -> ResolvedConfiguration:
    """Resolve declared defaults, workspace values, and explicit invocation values."""
    from zicato.workspace.config_schema import workspace_declaration  # noqa: PLC0415

    workspace_declaration(workspace_config)
    sections = {item.name for item in fields(ZicatoConfig)}
    authored = {key: value for key, value in workspace_config.items() if key in sections}
    integration = dict(authored.get("integration", {}))
    if not integration.get("harmonograf_url", "").strip() and workspace_config.get(
        "harmonograf_url"
    ):
        integration["harmonograf_url"] = workspace_config["harmonograf_url"]
        authored["integration"] = integration
    if "runtime" in authored:
        runtime = authored["runtime"]
        validate_authored_overlay(RuntimeDeclaration, runtime, path="config.runtime")
        operational_fields = {item.name for item in fields(RuntimeSettings)}
        authored["runtime"] = {
            key: value for key, value in runtime.items() if key in operational_fields
        }
    validate_authored_overlay(ZicatoConfig, authored, path="config")
    overrides = overlay.overrides if overlay is not None else {}
    values = authored_dataclass_from_json(ZicatoConfig, _merge(authored, overrides), path="config")
    sources = {path: "default" for path in _leaves(dataclass_to_jsonable(values))}
    sources.update({path: "workspace" for path in _leaves(authored)})
    sources.update({path: "invocation" for path in _leaves(overrides)})
    return ResolvedConfiguration(values, sources)
