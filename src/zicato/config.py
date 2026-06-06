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
3. The explicit ``overrides`` mapping passed to :func:`load_config`.

Programmatic construction
-------------------------

The dataclass tree is constructible directly, with no environment
involved at all::

    cfg = ZicatoConfig(health=HealthConfig(scoring_window=10))

That is the supported way for an embedding application to pin
configuration. :func:`load_config` is a convenience that layers the
environment underneath such an override.

Env-var compatibility
---------------------

Every ``ZICATO_*`` variable zicato has ever honoured is still honoured —
the names are simply all enumerated in :data:`_ENV_BINDINGS` rather than
scattered across the tree. To learn the full set, read that table (or
call :func:`describe_env_vars`).
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

    These thresholds drive :mod:`zicato.health.diagnostics`. Every one is
    a magnitude or a fraction where a non-positive (or, for fractions, a
    negative) value is meaningless; :func:`load_config` clamps invalid
    env input back to the default rather than letting an operator
    accidentally disable a detector.

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
        :func:`asyncio.wait_for` against this budget. A non-positive
        value is meaningless (it would short-circuit every call), so
        :func:`load_config` clamps it back to the default.
    """

    call_timeout_s: float = 120.0


@dataclass(frozen=True, slots=True)
class BudgetConfig:
    """Wall-clock budgeting for an evolve invocation.

    Fields
    ------
    max_wall_clock_seconds:
        Total wall-clock budget for a whole ``zicato evolve``
        invocation, in seconds, or ``None`` for an unbounded loop. The
        loop stops cleanly between rounds once the budget is spent;
        applies on top of each board entry's own
        ``wall_clock_budget_seconds``.
    """

    max_wall_clock_seconds: int | None = None


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
        :mod:`zicato.telemetry.harmonograf_supervisor`). Set this to opt
        out of auto-launch and stream to an external long-lived
        harmonograf instead — useful for collecting traffic from many
        zicato invocations into a single shared console. The full
        integration design (server lifecycle, the board-run vs meta-loop
        session taxonomy, and the two dashboard surfaces) lives in
        ``docs/design/HARMONOGRAF.md``.
    supervisor_binary:
        Filesystem path to the ``zicato-supervisor`` watchdog binary, or
        empty string to fall back to the in-tree release build and then
        the system ``PATH``. Mostly used by tests pointing at a sentinel
        script.
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
        ``supervisor/static`` directory. Useful for installed wheels
        that relocate the bundle and for tests.
    """

    static_dir: str = ""


@dataclass(frozen=True, slots=True)
class WorkspaceConfig:
    """Identity and location of the zicato workspace.

    Fields
    ------
    root:
        Path to the ``.zicato/`` workspace directory.
    instance_id:
        Logical instance identifier. Distinguishes nested instances when
        an outer zicato is optimizing an inner one.
    """

    root: str = ".zicato"
    instance_id: str = "default"


@dataclass(frozen=True, slots=True)
class RuntimeTuningConfig:
    """Operator-settable runtime tuning knobs.

    This sub-config exists so ``parallelism`` — historically hardcoded
    at :class:`zicato.core.types.RuntimeConfig`'s default and not
    settable through *any* mechanism — becomes a discoverable, typed,
    documented field.

    Note
    ----
    Wiring this value into :class:`zicato.core.types.RuntimeConfig` (the
    runtime/watchdog config) is intentionally out of this module's
    scope; the field is surfaced here so the knob is discoverable and a
    follow-up can thread it through the ``RuntimeConfig`` construction
    site.

    Fields
    ------
    parallelism:
        Maximum number of **board units** the tournament runner keeps in
        flight at once — "how many boards run in parallel". One board
        unit per board entry; in full mode a unit runs its champion and
        challenger runs concurrently (so ``parallelism`` units mean up to
        ``2 * parallelism`` subprocesses), in fast mode only the
        challenger. ``1`` admits one board unit at a time. Must be
        ``>= 1``.
    harness_call_timeout_ms:
        Per-LLM-call wall-clock budget, in milliseconds, for the *inner
        harness* agent's calls — distinct from
        :attr:`AuxConfig.call_timeout_s` (the auxiliary-LLM budget).
        goldfive's :class:`~goldfive.config.AgentConfig` defaults this
        to 120 000 ms, which a real reasoning model legitimately
        exceeds on a long prompt under concurrency; zicato raises the
        default to a value sized for reasoning-model latency and
        threads it into the goldfive ``RuntimeConfig`` it constructs
        for every ``goldfive.run`` call. Operators tune via
        ``ZICATO_HARNESS_CALL_TIMEOUT_MS``. Must be ``>= 1``.
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
    budget: BudgetConfig = BudgetConfig()
    integration: IntegrationConfig = IntegrationConfig()
    dashboard: DashboardConfig = DashboardConfig()
    workspace: WorkspaceConfig = WorkspaceConfig()
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


def _coerce_positive_float(raw: str, default: float) -> float:
    """Parse a strictly-positive float; fall back to ``default`` on invalid input.

    A non-positive value is invalid: the only consumer is the auxiliary
    call timeout, where a 0-second budget would short-circuit every
    call.
    """
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0.0 else default


def _coerce_optional_positive_int(raw: str, default: int | None) -> int | None:
    """Parse a strictly-positive int, or keep ``default`` (which may be ``None``).

    The only consumer is the wall-clock budget, where ``None`` means
    "unbounded" and any concrete value must be ``>= 1``.
    """
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _coerce_str(raw: str, _default: str) -> str:
    """Pass a string through verbatim — the identity coercion.

    Present so every binding in :data:`_ENV_BINDINGS` has a uniform
    ``(raw, default) -> value`` coercer signature.
    """
    return raw


# ---------------------------------------------------------------------------
# The single enumeration of every ZICATO_* environment variable
# ---------------------------------------------------------------------------

#: Every ``ZICATO_*`` environment variable zicato honours, in one place.
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
#: environment. To add a new env-settable knob, add a field to the
#: relevant sub-config and one row here.
_ENV_BINDINGS: dict[str, tuple[str, str, Any]] = {
    "ZICATO_HEALTH_SCORING_WINDOW": ("health", "scoring_window", _coerce_positive_int),
    "ZICATO_HEALTH_SCORING_EPSILON": ("health", "scoring_epsilon", _coerce_non_negative_float),
    "ZICATO_HEALTH_NO_EXPECTATIONS_FRACTION": (
        "health",
        "no_expectations_fraction",
        _coerce_non_negative_float,
    ),
    "ZICATO_HEALTH_STALLED_REJECTS": ("health", "stalled_rejects", _coerce_positive_int),
    "ZICATO_HEALTH_GENERALIZATION_GAP_WARN": (
        "health",
        "generalization_gap_warn",
        _coerce_non_negative_float,
    ),
    "ZICATO_HEALTH_GENERALIZATION_GAP_CRIT": (
        "health",
        "generalization_gap_crit",
        _coerce_non_negative_float,
    ),
    "ZICATO_AUX_CALL_TIMEOUT": ("aux", "call_timeout_s", _coerce_positive_float),
    "ZICATO_MAX_WALL_CLOCK_SECONDS": (
        "budget",
        "max_wall_clock_seconds",
        _coerce_optional_positive_int,
    ),
    "ZICATO_HARMONOGRAF_URL": ("integration", "harmonograf_url", _coerce_str),
    "ZICATO_SUPERVISOR_BINARY": ("integration", "supervisor_binary", _coerce_str),
    "ZICATO_DASHBOARD_STATIC_DIR": ("dashboard", "static_dir", _coerce_str),
    "ZICATO_WORKSPACE": ("workspace", "root", _coerce_str),
    "ZICATO_INSTANCE_ID": ("workspace", "instance_id", _coerce_str),
    "ZICATO_PARALLELISM": ("runtime", "parallelism", _coerce_positive_int),
    "ZICATO_HARNESS_CALL_TIMEOUT_MS": (
        "runtime",
        "harness_call_timeout_ms",
        _coerce_positive_int,
    ),
}


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
    3. ``overrides`` — an explicit nested ``{section: {field: value}}``
       mapping. Values here win over both defaults and the environment;
       this is how an embedding application pins configuration on top of
       whatever the environment supplies.

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
    if overrides:
        config = _apply_overrides(config, overrides)
    return config


__all__ = [
    "HealthConfig",
    "AuxConfig",
    "BudgetConfig",
    "IntegrationConfig",
    "DashboardConfig",
    "WorkspaceConfig",
    "RuntimeTuningConfig",
    "ZicatoConfig",
    "load_config",
    "describe_env_vars",
]
