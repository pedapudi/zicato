"""The typed, discoverable configuration tree for zicato.

Zicato's tunable knobs live in a single frozen dataclass tree rather than
in ``ZICATO_*`` environment variables read through scattered
``os.environ.get(...)`` calls. An environment-variable surface is
undiscoverable (a reader has to grep the tree to learn a knob exists),
untyped (every site re-implements its own string→number coercion), and not
programmatically settable (a caller embedding zicato cannot override a knob
without mutating ``os.environ``).

:class:`ZicatoConfig` composes nested domain sub-configs — every tunable
knob is a typed field with a default and a docstring, so the
configuration surface is the dataclass definition itself.

Loading
-------

:func:`load_config` builds the tree from the dataclass defaults, layers
the process-pinned CLI-flag values (:func:`pin_overrides`) on top, and
applies any explicit ``overrides`` last. It does NOT read the
environment: every operator knob is a CLI flag or a workspace
``config.json`` block, and downstream code takes the config object (or the
relevant sub-config) as a parameter rather than touching ``os.environ``.

Precedence, lowest to highest:

1. The dataclass field defaults.
2. Process-pinned overrides (:func:`pin_overrides`) — how CLI flags
   land on the tree: a command pins the flag values once at startup and
   every later :func:`load_config` call in the process sees them.
3. The explicit ``overrides`` mapping passed to :func:`load_config`.

Programmatic construction
-------------------------

The dataclass tree is constructible directly, with no loader
involved at all::

    cfg = ZicatoConfig(health=HealthConfig(scoring_window=10))

That is the supported way for an embedding application to pin
configuration. :func:`load_config` is a convenience that layers the
pinned CLI-flag values underneath such an override.

CLI flags → the config tree
---------------------------

Operator knobs are CLI flags (plus, for some knobs, a workspace
``config.json`` block) — not environment variables. A flag reaches the
deep call sites that re-resolve configuration via :func:`load_config`
through :func:`pin_overrides`: the CLI command validates and pins the
flag values once at startup, and every subsequent :func:`load_config`
in the process layers those pins on top of the defaults. The
tournament runner threads the pins across the worker subprocess
boundary in the worker args file, so a flag like ``--aux-call-timeout``
is honoured inside the worker where the value is actually consumed.

Env-var surface
---------------

:func:`load_config` honours NO environment variables; the former
operator-env surface was deleted outright:

* the redundant trio ``ZICATO_MAX_WALL_CLOCK_SECONDS`` /
  ``ZICATO_WORKSPACE`` / ``ZICATO_INSTANCE_ID`` (each fully shadowed by
  an existing CLI flag);
* the six flag-converted knobs (``ZICATO_AUX_CALL_TIMEOUT``,
  ``ZICATO_PARALLELISM``, ``ZICATO_HARNESS_CALL_TIMEOUT_MS``,
  ``ZICATO_SUPERVISOR_BINARY``, ``ZICATO_DASHBOARD_STATIC_DIR``,
  ``ZICATO_HARMONOGRAF_URL`` — the last surviving only as the internal
  auto-launch handoff read by :mod:`zicato.telemetry.sink`);
* the six ``ZICATO_HEALTH_*`` thresholds, moved to the ``health`` block
  of the workspace ``config.json``.

What remains is a small MERITED set of environment variables zicato
touches — each one an actual process-boundary contract,
not a configuration knob: the per-run harness contract
(``ZICATO_RUN_SCRATCH_DIR``), the internal harmonograf handoff pair,
the secrets boundary (operator-NAMED ``api_key_env`` variables and the
``runtime.worker_env_passthrough`` allowlist), and two CI/test toggles.
The set is enumerated (with role labels) by :func:`describe_env_vars`
and surfaced by ``zicato inspect environment``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from typing import Any

# ---------------------------------------------------------------------------
# Domain sub-configs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HealthConfig:
    """Tuning knobs for the evolve-loop health detectors.

    These thresholds drive :mod:`zicato.health.diagnostics`. Operators
    tune them in the workspace ``config.json``'s ``health`` block::

        {
          "health": {
            "scoring_window": 5,
            "scoring_epsilon": 1e-6,
            "no_expectations_fraction": 0.5,
            "stalled_rejects": 3,
            "generalization_gap_warn": 0.05,
            "generalization_gap_crit": 0.15
          }
        }

    which :func:`health_config_from_workspace` parses into this
    dataclass (the orchestrator's per-round health assessment and the
    ``zicato health`` command both route through it). Every threshold is
    a magnitude or a fraction where a non-positive (or, for fractions, a
    negative) value is meaningless; the parser clamps an invalid value
    back to the field default rather than letting an operator
    accidentally disable a detector, while an *unknown key* raises so a
    typo fails loudly.

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
        A fraction in ``[0, 1]``.
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
        clamped to at least ``generalization_gap_warn`` so the critical bar
        never sits below the warning bar.
    """

    scoring_window: int = 3
    scoring_epsilon: float = 1e-6
    no_expectations_fraction: float = 0.5
    stalled_rejects: int = 3
    generalization_gap_warn: float = 0.05
    generalization_gap_crit: float = 0.15


@dataclass(frozen=True, slots=True)
class AuxConfig:
    """Configuration for auxiliary-LLM calls (proposer / judge / analysis).

    Fields
    ------
    call_timeout_s:
        Per-call wall-clock budget, in seconds, for every auxiliary-LLM
        invocation. A hung auxiliary endpoint can wedge a round; each
        call site wraps its ``aux_call_llm`` invocation in
        :func:`asyncio.wait_for` against this budget. Operators tune it
        with ``zicato evolve --aux-call-timeout``. A non-positive value
        is meaningless — it would short-circuit every call — and the
        flag rejects it up front.
    """

    call_timeout_s: float = 120.0


@dataclass(frozen=True, slots=True)
class IntegrationConfig:
    """Paths and URLs for the processes zicato integrates with.

    Fields
    ------
    harmonograf_url:
        URL of an external harmonograf server to stream this evolve
        invocation's telemetry to. Empty string (the default) means
        zicato auto-launches its own harmonograf bound to a free
        localhost port at evolve startup (see
        :mod:`zicato.telemetry.harmonograf_supervisor`). Operators set
        it with ``zicato evolve --harmonograf-url`` (or the workspace
        ``config.json`` key ``harmonograf_url``) to opt out of
        auto-launch and stream to an external long-lived harmonograf
        instead — useful for collecting traffic from many zicato
        invocations into a single shared console. The full integration
        design (server lifecycle, the board-run vs meta-loop session
        taxonomy, and the two dashboard surfaces) lives in
        ``docs/design/HARMONOGRAF.md``. Note the auto-launch path also
        broadcasts the resolved URL through the INTERNAL
        ``ZICATO_HARMONOGRAF_URL`` handoff channel — see
        :mod:`zicato.telemetry.sink` — which is not an operator surface.
    supervisor_binary:
        Filesystem path to the ``zicato-supervisor`` watchdog binary, or
        empty string to fall back to the in-tree release build and then
        the system ``PATH``. Operators set it with ``zicato evolve
        --supervisor-binary``; mostly used by tests pointing at a
        sentinel script.
    """

    harmonograf_url: str = ""
    supervisor_binary: str = ""


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


@dataclass(frozen=True, slots=True)
class RuntimeTuningConfig:
    """Operator-settable runtime tuning knobs.

    This sub-config is what makes ``parallelism`` a discoverable, typed,
    documented field rather than a value pinned at
    :class:`zicato.core.types.RuntimeConfig`'s default with no way to set
    it. :func:`zicato.runtime_factory.make_runtime_config`
    threads it into the :class:`zicato.core.types.RuntimeConfig` it
    builds.

    Fields
    ------
    parallelism:
        Maximum number of **board units** the tournament runner keeps in
        flight at once — "how many boards run in parallel". One board
        unit per board entry; in full mode a unit runs its champion and
        challenger runs concurrently (so ``parallelism`` units mean up to
        ``2 * parallelism`` subprocesses), in fast mode only the
        challenger. ``1`` admits one board unit at a time. Operators
        tune it with ``zicato evolve --parallelism`` (or the workspace
        ``config.json``'s ``runtime.parallelism``; the flag wins). Must
        be ``>= 1``.
    """

    parallelism: int = 4


# ---------------------------------------------------------------------------
# The root config
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ZicatoConfig:
    """The root of zicato's typed configuration tree.

    Composes every domain sub-config. Construct it directly to pin
    configuration programmatically::

        ZicatoConfig(health=HealthConfig(scoring_window=10))

    or call :func:`load_config` to layer process-pinned CLI values and an
    optional set of explicit overrides over the documented defaults.
    """

    health: HealthConfig = HealthConfig()
    aux: AuxConfig = AuxConfig()
    integration: IntegrationConfig = IntegrationConfig()
    dashboard: DashboardConfig = DashboardConfig()
    runtime: RuntimeTuningConfig = RuntimeTuningConfig()


# ---------------------------------------------------------------------------
# Env-var coercion helpers
# ---------------------------------------------------------------------------


def _coerce_positive_int(raw: str, default: int) -> int:
    """Parse a strictly-positive int; fall back to ``default`` on invalid input.

    A missing, unparseable, or non-positive value yields ``default`` —
    every consumer of this coercion treats a sub-one value as a
    programming error (a zero health window silently disables a
    detector).
    """
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _coerce_non_negative_float(raw: str, default: float) -> float:
    """Parse a non-negative float; fall back to ``default`` on invalid input.

    A negative value is treated as invalid — every threshold fed by this
    coercion is a magnitude or a fraction where negative is meaningless.
    """
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0.0 else default


#: Coercer per :class:`HealthConfig` field for the workspace
#: ``config.json`` ``health`` block. The same clamp-to-default coercers
#: the env bindings used (they accept JSON numbers as well as strings),
#: so an out-of-range value degrades to the field default instead of
#: disabling a detector. The former ``ZICATO_HEALTH_*`` env vars are
#: deleted; this block is the operator surface.
_HEALTH_FIELD_COERCERS: dict[str, Any] = {
    "scoring_window": _coerce_positive_int,
    "scoring_epsilon": _coerce_non_negative_float,
    "no_expectations_fraction": _coerce_non_negative_float,
    "stalled_rejects": _coerce_positive_int,
    "generalization_gap_warn": _coerce_non_negative_float,
    "generalization_gap_crit": _coerce_non_negative_float,
}


def health_config_from_workspace(workspace_config: Mapping[str, Any] | None) -> HealthConfig:
    """Build the :class:`HealthConfig` from a workspace ``config.json`` dict.

    Reads the optional top-level ``health`` block — the operator surface
    for the loop-health detector thresholds (the former
    ``ZICATO_HEALTH_*`` env vars, deleted). Both health call sites route
    through here: the orchestrator's per-round assessment and the
    ``zicato health`` command.

    Semantics:

    * an absent / ``None`` config, or a config without a ``health``
      block, yields the fully-defaulted :class:`HealthConfig`;
    * a ``health`` block that is not a JSON object raises ``ValueError``
      (fail loudly, like every other malformed config block);
    * an unknown key raises ``KeyError`` naming the valid fields, so a
      typo cannot silently leave a detector on its default;
    * each value runs through the same clamp-to-default coercer the env
      binding used — an unparseable or out-of-range value falls back to
      the field default rather than disabling a detector.
    """
    if not workspace_config:
        return HealthConfig()
    raw = workspace_config.get("health")
    if raw is None:
        return HealthConfig()
    if not isinstance(raw, Mapping):
        raise ValueError(
            f"workspace config.json 'health' block must be a JSON object, "
            f"got {type(raw).__name__}"
        )
    unknown = set(raw) - set(_HEALTH_FIELD_COERCERS)
    if unknown:
        raise KeyError(
            f"unknown field(s) {sorted(unknown)} in the workspace config.json "
            f"'health' block; known fields: {sorted(_HEALTH_FIELD_COERCERS)}"
        )
    defaults = HealthConfig()
    values: dict[str, Any] = {}
    for field_name, coerce in _HEALTH_FIELD_COERCERS.items():
        if field_name in raw:
            values[field_name] = coerce(raw[field_name], getattr(defaults, field_name))
    return replace(defaults, **values)


# ---------------------------------------------------------------------------
# The merited env-var set — the environment variables zicato still touches
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EnvVarInfo:
    """One retained environment variable, with its role.

    Fields
    ------
    name:
        The variable name, or an ``<angle-bracketed>`` placeholder for a
        family whose concrete names the operator chooses in
        ``config.json`` or ``scoring.json`` (the secrets boundary).
    role:
        Why an environment variable is the RIGHT mechanism here — one of
        ``"harness-contract"``, ``"internal-handoff"``,
        ``"external-integration"``, ``"secrets-boundary"``, or
        ``"test-toggle"``. Configuration
        knobs are none of these; they live on CLI flags and in workspace
        configuration or contract files.
    description:
        Who sets it, who reads it, and what crosses the boundary.
    """

    name: str
    role: str
    description: str


#: The small merited set, one entry per variable (or operator-named
#: family). Every entry is a process-boundary contract — a value that
#: must cross between processes (orchestrator ↔ worker ↔ inner harness ↔
#: sibling tools) where an environment variable is the honest mechanism —
#: never an operator tuning knob. Operator knobs are CLI flags and
#: ``config.json`` blocks; see the module docstring.
_MERITED_ENV_VARS: tuple[EnvVarInfo, ...] = (
    EnvVarInfo(
        name="ZICATO_RUN_SCRATCH_DIR",
        role="harness-contract",
        description=(
            "Set BY the tournament worker FOR the inner harness: a fresh "
            "per-run scratch directory the harness must route its runtime "
            "output to, so run artifacts never pollute the generation code "
            "snapshot (zicato.epoch.snapshot_scope.SCRATCH_DIR_ENV)."
        ),
    ),
    EnvVarInfo(
        name="ZICATO_HARMONOGRAF_URL",
        role="internal-handoff",
        description=(
            "Set by the evolve loop's harmonograf AUTO-LAUNCH to broadcast "
            "the launched console's web URL to every downstream re-resolver "
            "(in-process call sites and tournament worker subprocesses); "
            "restored to its prior state at shutdown. NOT an operator knob — "
            "operators use `zicato evolve --harmonograf-url` or the "
            "config.json harmonograf_url key, both of which outrank it."
        ),
    ),
    EnvVarInfo(
        name="ZICATO_HARMONOGRAF_GRPC",
        role="internal-handoff",
        description=(
            "Companion to ZICATO_HARMONOGRAF_URL on the auto-launch path: "
            "the native gRPC host:port the per-run telemetry sinks must "
            "dial (the web URL serves gRPC-Web on a different port). Set "
            "and restored by the same lifecycle."
        ),
    ),
    EnvVarInfo(
        name="ZICATO_PROPOSER_TOOL_CONTEXT",
        role="internal-handoff",
        description=(
            "Set by the proposer launcher for the isolated MCP tool server: "
            "names the per-round JSON context file that the child process reads."
        ),
    ),
    EnvVarInfo(
        name="PI_CODING_AGENT_DIR",
        role="external-integration",
        description=(
            "Read as the operator's Pi credential source, then set for each "
            "isolated Pi proposer process to name its private agent directory."
        ),
    ),
    EnvVarInfo(
        name="PI_CODING_AGENT_SESSION_DIR",
        role="external-integration",
        description=(
            "Set for an isolated Pi proposer process to keep its sessions under "
            "the per-challenger agent directory."
        ),
    ),
    EnvVarInfo(
        name="PI_OFFLINE",
        role="external-integration",
        description=(
            "Set to 1 for an isolated Pi proposer process so it uses the model "
            "and credentials Zicato supplied without remote package discovery."
        ),
    ),
    EnvVarInfo(
        name="<models.<role>.api_key_env>",
        role="secrets-boundary",
        description=(
            "Operator-NAMED variables holding provider credentials: each "
            "model role in config.json's models block names the variable "
            "(api_key_env) whose VALUE the worker reads to authenticate. "
            "Credentials stay in the environment — never in config files — "
            "and the worker env-scrub keeps exactly the named variables."
        ),
    ),
    EnvVarInfo(
        name="<scoring.goldfive.{embedding,judge}.api_key_env>",
        role="secrets-boundary",
        description=(
            "Operator-named variables holding credentials for Goldfive's "
            "embedding or built-in-judge endpoint. scoring.json stores and "
            "hashes each variable NAME; the scrubbed tournament worker keeps "
            "that named variable and resolves its VALUE only when it builds "
            "the Goldfive runtime."
        ),
    ),
    EnvVarInfo(
        name="<runtime.worker_env_passthrough>",
        role="secrets-boundary",
        description=(
            "Operator-named allowlist (a config.json runtime key) of extra "
            "environment variables a scrubbed worker still receives — the "
            "escape hatch for a target that reads a bespoke variable."
        ),
    ),
    EnvVarInfo(
        name="ZICATO_SKIP_HOOK_CHECK",
        role="test-toggle",
        description=(
            "CI/test toggle: skips the test asserting the repo's git hooks "
            "are installed (for environments that manage hooks differently)."
        ),
    ),
    EnvVarInfo(
        name="ZICATO_PARITY_UPDATE",
        role="test-toggle",
        description=(
            "CI/test toggle: set to 1 to re-capture the dashboard "
            "reader-parity golden instead of diffing against it."
        ),
    ),
)


def describe_env_vars() -> tuple[EnvVarInfo, ...]:
    """Return the merited set of environment variables zicato touches.

    The introspection helper behind ``zicato inspect environment``. Since the
    env-var rationalization, NO environment variable is a configuration
    knob — operator knobs live on CLI flags and in the workspace
    ``config.json`` — so this describes only the retained
    process-boundary contracts, each labelled with the role that makes
    an environment variable the right mechanism for it.
    """
    return _MERITED_ENV_VARS


# ---------------------------------------------------------------------------
# The loader
# ---------------------------------------------------------------------------


def _apply_overrides(config: ZicatoConfig, overrides: Mapping[str, Any]) -> ZicatoConfig:
    """Return ``config`` with ``overrides`` applied on top.

    ``overrides`` is a nested mapping ``{section: {field: value}}``. Only
    known sections and fields are accepted; an unknown key is a
    programming error and raises ``KeyError`` / ``TypeError`` so a typo
    fails loudly rather than being silently ignored.
    """
    section_names = {f.name for f in fields(ZicatoConfig)}
    section_updates: dict[str, Any] = {}
    for section_name, field_values in overrides.items():
        if section_name not in section_names:
            raise KeyError(
                f"unknown config section {section_name!r}; known sections: {sorted(section_names)}"
            )
        if not isinstance(field_values, Mapping):
            raise TypeError(
                f"override for section {section_name!r} must be a mapping of "
                f"field->value, got {type(field_values).__name__}"
            )
        current = getattr(config, section_name)
        valid_fields = {f.name for f in fields(current)}
        unknown = set(field_values) - valid_fields
        if unknown:
            raise KeyError(
                f"unknown field(s) {sorted(unknown)} for config section "
                f"{section_name!r}; known fields: {sorted(valid_fields)}"
            )
        section_updates[section_name] = replace(current, **dict(field_values))
    if not section_updates:
        return config
    return replace(config, **section_updates)


# ---------------------------------------------------------------------------
# Process-pinned overrides — how CLI flags land on the tree
# ---------------------------------------------------------------------------

#: Process-wide pinned overrides, ``{section: {field: value}}``. Written
#: only by :func:`pin_overrides` (the CLI commands, and the tournament
#: worker re-pinning the values its args file carried across the process
#: boundary); read by :func:`load_config`, which layers them on top of
#: the dataclass defaults.
_PINNED_OVERRIDES: dict[str, dict[str, Any]] = {}


def pin_overrides(overrides: Mapping[str, Mapping[str, Any]]) -> None:
    """Pin ``{section: {field: value}}`` overrides for the whole process.

    This is the bridge from CLI flags to the config tree: a command
    validates and pins its flag values once at startup, and every later
    :func:`load_config` call — however deep in the call graph — sees
    them layered on top of the dataclass defaults (explicit ``overrides``
    passed to :func:`load_config` still win).

    Validation is eager: an unknown section or field raises immediately
    (via the same check :func:`load_config` applies), so a typo fails at
    the pin site rather than surfacing as a silently-defaulted knob
    later. Repeated calls merge field-by-field; the latest pin of a
    field wins.

    The tournament runner serialises the current pins into every worker
    args file and the worker re-pins them at startup, so a pinned knob
    consumed inside the worker subprocess (for example, board-unit
    parallelism) crosses the process boundary without an environment variable.
    """
    # Validate loudly before mutating any state.
    _apply_overrides(ZicatoConfig(), overrides)
    for section_name, field_values in overrides.items():
        _PINNED_OVERRIDES.setdefault(section_name, {}).update(dict(field_values))


def get_pinned_overrides() -> dict[str, dict[str, Any]]:
    """Return a deep copy of the process-pinned overrides.

    Used by the tournament runner to thread the pins across the worker
    subprocess boundary (the copy is JSON-serialisable as long as pinned
    values are, which every CLI-flag value is), and by tests.
    """
    return {section: dict(fields_map) for section, fields_map in _PINNED_OVERRIDES.items()}


def pinned_override(section: str, field: str) -> Any | None:
    """Return the pinned value for ``section.field``, or ``None`` if unpinned.

    For the rare call site that must distinguish "the operator
    explicitly pinned this knob" from "the knob is at its default" —
    e.g. :func:`zicato.runtime_factory.make_runtime_config`, where an
    explicit ``--parallelism`` flag outranks the workspace
    ``config.json`` value but the mere default must not.
    """
    return _PINNED_OVERRIDES.get(section, {}).get(field)


def clear_pinned_overrides() -> None:
    """Drop every process-pinned override (test isolation)."""
    _PINNED_OVERRIDES.clear()


def load_config(
    *,
    overrides: Mapping[str, Any] | None = None,
) -> ZicatoConfig:
    """Build a :class:`ZicatoConfig` from defaults + pins + ``overrides``.

    Reads NO environment variables — operator knobs are CLI flags
    (landing here via :func:`pin_overrides`) and workspace
    ``config.json`` blocks. Downstream code takes the returned
    :class:`ZicatoConfig` (or one of its sub-configs) as a parameter
    rather than touching ``os.environ``.

    Precedence, lowest to highest:

    1. The dataclass field defaults.
    2. Process-pinned overrides (:func:`pin_overrides`) — CLI-flag
       values pinned once at command startup (and re-pinned by the
       tournament worker from its args file).
    3. ``overrides`` — an explicit nested ``{section: {field: value}}``
       mapping. Values here win over everything; this is how an
       embedding application pins configuration on top of whatever the
       CLI supplied.

    Parameters
    ----------
    overrides:
        Optional ``{section: {field: value}}`` mapping applied last. An
        unknown section or field name raises rather than being ignored.

    Returns
    -------
    ZicatoConfig
        A fully-typed, frozen configuration tree.
    """
    config = ZicatoConfig()
    if _PINNED_OVERRIDES:
        config = _apply_overrides(config, _PINNED_OVERRIDES)
    if overrides:
        config = _apply_overrides(config, overrides)
    return config


__all__ = [
    "HealthConfig",
    "AuxConfig",
    "IntegrationConfig",
    "DashboardConfig",
    "RuntimeTuningConfig",
    "ZicatoConfig",
    "EnvVarInfo",
    "load_config",
    "health_config_from_workspace",
    "describe_env_vars",
    "pin_overrides",
    "get_pinned_overrides",
    "pinned_override",
    "clear_pinned_overrides",
]
