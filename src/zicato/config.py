"""The typed, discoverable configuration tree for zicato.

Zicato has historically read a dozen-plus ``ZICATO_*`` environment
variables through scattered ``os.environ.get(...)`` calls at point of
use. That surface is undiscoverable (you have to grep the tree to learn
it exists), untyped (every site re-implements its own string→number
coercion), and not programmatically settable (a caller embedding zicato
cannot override a knob without mutating ``os.environ``).

This module replaces that surface with a single frozen dataclass tree.
:class:`ZicatoConfig` composes nested domain sub-configs — every tunable
knob is a typed field with a default and a docstring, so the
configuration surface is the dataclass definition itself.

Loading
-------

:func:`load_config` is the *only* place in zicato that reads the
environment. It reads ``env`` exactly once, treats it as the
lowest-priority override layer, applies any explicit ``overrides`` on
top, and returns a fully-typed :class:`ZicatoConfig`. Downstream code
takes the config object (or the relevant sub-config) as a parameter and
never touches ``os.environ`` itself.

Precedence, lowest to highest:

1. The dataclass field defaults.
2. The environment (``ZICATO_*`` variables — all enumerated in
   :data:`_ENV_BINDINGS`).
3. Process-pinned overrides (:func:`pin_overrides`) — how CLI flags
   land on the tree: a command pins the flag values once at startup and
   every later :func:`load_config` call in the process sees them.
4. The explicit ``overrides`` mapping passed to :func:`load_config`.

Programmatic construction
-------------------------

The dataclass tree is constructible directly, with no environment
involved at all::

    cfg = ZicatoConfig(health=HealthConfig(scoring_window=10))

That is the supported way for an embedding application to pin
configuration. :func:`load_config` is a convenience that layers the
environment underneath such an override.

CLI flags → the config tree
---------------------------

Operator knobs are CLI flags (plus, for some knobs, a workspace
``config.json`` block) — not environment variables. A flag reaches the
deep call sites that re-resolve configuration via :func:`load_config`
through :func:`pin_overrides`: the CLI command validates and pins the
flag values once at startup, and every subsequent :func:`load_config`
in the process layers those pins on top of the environment. The
tournament runner threads the pins across the worker subprocess
boundary in the worker args file, so a flag like
``--harness-call-timeout-ms`` is honoured inside the worker where the
value is actually consumed.

Env-var surface
---------------

Operator knobs are NOT environment variables: they live on CLI flags
(pinned here via :func:`pin_overrides`) and in the workspace
``config.json`` (the ``health`` block —
:func:`health_config_from_workspace` — plus the ``runtime`` /
``harmonograf_url`` keys read by their own loaders). Every
``ZICATO_*`` variable :func:`load_config` still honours is enumerated
in :data:`_ENV_BINDINGS`; a variable absent from that table is ignored.
The former operator-env surface was deleted outright:

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
"""

from __future__ import annotations

import os
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
        ``zicato dashboard --static-dir`` / ``zicato builder
        --static-dir``. Useful for installed wheels that relocate the
        bundle and for tests.
    """

    static_dir: str = ""


@dataclass(frozen=True, slots=True)
class RuntimeTuningConfig:
    """Operator-settable runtime tuning knobs.

    This sub-config exists so ``parallelism`` — historically hardcoded
    at :class:`zicato.core.types.RuntimeConfig`'s default and not
    settable through *any* mechanism — becomes a discoverable, typed,
    documented field. :func:`zicato.runtime_factory.make_runtime_config`
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
    harness_call_timeout_ms:
        Per-LLM-call wall-clock budget, in milliseconds, for the *inner
        harness* agent's calls — distinct from
        :attr:`AuxConfig.call_timeout_s` (the auxiliary-LLM budget).
        goldfive's :class:`~goldfive.config.AgentConfig` defaults this
        to 120 000 ms, which a real reasoning model legitimately
        exceeds on a long prompt under concurrency; zicato raises the
        default to a value sized for reasoning-model latency and
        threads it into the goldfive ``RuntimeConfig`` it constructs
        for every ``goldfive.run`` call. Operators tune it with
        ``zicato evolve --harness-call-timeout-ms``. Must be ``>= 1``.
    """

    parallelism: int = 4
    harness_call_timeout_ms: int = 1_800_000


# ---------------------------------------------------------------------------
# The root config
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ZicatoConfig:
    """The root of zicato's typed configuration tree.

    Composes every domain sub-config. Construct it directly to pin
    configuration programmatically::

        ZicatoConfig(health=HealthConfig(scoring_window=10))

    or call :func:`load_config` to layer the environment underneath an
    optional set of explicit overrides.
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


# ---------------------------------------------------------------------------
# The single enumeration of every ZICATO_* environment variable
# ---------------------------------------------------------------------------

#: Every ``ZICATO_*`` environment variable :func:`load_config` honours,
#: in one place.
#:
#: Each entry maps an env-var name to ``(section, field, coercer)``:
#:
#: * ``section`` — the :class:`ZicatoConfig` attribute holding the
#:   sub-config (e.g. ``"health"``).
#: * ``field`` — the field on that sub-config the variable sets.
#: * ``coercer`` — a ``(raw_str, default) -> value`` function that
#:   parses the env string and clamps invalid input to the default.
#:
#: :func:`load_config` walks this table; nothing else reads the
#: environment. The table only shrinks: operator knobs live on CLI
#: flags (landing here via :func:`pin_overrides`) and in the workspace
#: ``config.json``, not in new environment variables. The former
#: operator-env knobs (``ZICATO_AUX_CALL_TIMEOUT``,
#: ``ZICATO_PARALLELISM``, ``ZICATO_HARNESS_CALL_TIMEOUT_MS``,
#: ``ZICATO_SUPERVISOR_BINARY``, ``ZICATO_DASHBOARD_STATIC_DIR``,
#: ``ZICATO_HARMONOGRAF_URL``) were deleted in favour of flags — a set
#: variable is simply ignored. (``ZICATO_HARMONOGRAF_URL`` survives
#: ONLY as the internal auto-launch handoff channel read by
#: :mod:`zicato.telemetry.sink`, not as a ``load_config`` binding.)
_ENV_BINDINGS: dict[str, tuple[str, str, Any]] = {}


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


def describe_env_vars() -> dict[str, str]:
    """Return ``{env_var_name: "section.field"}`` for every honoured variable.

    A small introspection helper for the CLI and for documentation: the
    configuration surface is discoverable without grepping the tree.
    """
    return {name: f"{section}.{field}" for name, (section, field, _) in _ENV_BINDINGS.items()}


# ---------------------------------------------------------------------------
# The loader — the one place that reads the environment
# ---------------------------------------------------------------------------


def _apply_overrides(config: ZicatoConfig, overrides: Mapping[str, Any]) -> ZicatoConfig:
    """Return ``config`` with ``overrides`` applied on top.

    ``overrides`` is a nested mapping ``{section: {field: value}}`` —
    the same shape :func:`load_config` builds from the environment. Only
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
#: the environment.
_PINNED_OVERRIDES: dict[str, dict[str, Any]] = {}


def pin_overrides(overrides: Mapping[str, Mapping[str, Any]]) -> None:
    """Pin ``{section: {field: value}}`` overrides for the whole process.

    This is the bridge from CLI flags to the config tree: a command
    validates and pins its flag values once at startup, and every later
    :func:`load_config` call — however deep in the call graph — sees
    them layered on top of the environment (explicit ``overrides``
    passed to :func:`load_config` still win).

    Validation is eager: an unknown section or field raises immediately
    (via the same check :func:`load_config` applies), so a typo fails at
    the pin site rather than surfacing as a silently-defaulted knob
    later. Repeated calls merge field-by-field; the latest pin of a
    field wins.

    The tournament runner serialises the current pins into every worker
    args file and the worker re-pins them at startup, so a pinned knob
    consumed inside the worker subprocess (e.g. the harness call
    timeout) crosses the process boundary without an environment
    variable.
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
    env: Mapping[str, str] = os.environ,
    overrides: Mapping[str, Any] | None = None,
) -> ZicatoConfig:
    """Build a :class:`ZicatoConfig`, reading the environment exactly once.

    This is the **only** function in zicato that reads the environment.
    Downstream code takes the returned :class:`ZicatoConfig` (or one of
    its sub-configs) as a parameter and never touches ``os.environ``
    itself.

    Precedence, lowest to highest:

    1. The dataclass field defaults.
    2. ``env`` — every ``ZICATO_*`` variable in :data:`_ENV_BINDINGS`.
       Invalid values (unparseable, or out of a field's meaningful
       range) are clamped back to the default by the field's coercer.
    3. Process-pinned overrides (:func:`pin_overrides`) — CLI-flag
       values pinned once at command startup.
    4. ``overrides`` — an explicit nested ``{section: {field: value}}``
       mapping. Values here win over everything; this is how an
       embedding application pins configuration on top of whatever the
       environment and the CLI supply.

    Parameters
    ----------
    env:
        The environment mapping to read. Defaults to ``os.environ``;
        tests pass a plain dict to get full isolation.
    overrides:
        Optional ``{section: {field: value}}`` mapping applied last. An
        unknown section or field name raises rather than being ignored.

    Returns
    -------
    ZicatoConfig
        A fully-typed, frozen configuration tree.
    """
    section_updates: dict[str, dict[str, Any]] = {}
    for env_name, (section, field, coerce) in _ENV_BINDINGS.items():
        raw = env.get(env_name)
        if raw is None:
            continue
        default = getattr(getattr(ZicatoConfig(), section), field)
        section_updates.setdefault(section, {})[field] = coerce(raw, default)

    config = ZicatoConfig()
    if section_updates:
        config = _apply_overrides(config, section_updates)
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
    "load_config",
    "health_config_from_workspace",
    "describe_env_vars",
    "pin_overrides",
    "get_pinned_overrides",
    "pinned_override",
    "clear_pinned_overrides",
]
