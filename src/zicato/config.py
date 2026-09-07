"""Configuration declarations, explicit invocation overlays, and environment inventory.

Operational settings resolve from declared defaults, workspace values, and an
immutable invocation overlay. Callers pass the resulting configuration into
runtime construction and service owners. Historical record decoders are
separate from authored configuration admission.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from zicato.core.settings import (
    AuxConfig,
    DashboardConfig,
    HealthConfig,
    IntegrationConfig,
    InvocationOverlay,
    ResolvedConfiguration,
    ZicatoConfig,
    resolve_configuration,
)
from zicato.core.settings import (
    RuntimeSettings as RuntimeTuningConfig,
)

# ---------------------------------------------------------------------------
# Env-var coercion helpers
# ---------------------------------------------------------------------------


def health_config_from_workspace(workspace_config: Mapping[str, Any] | None) -> HealthConfig:
    """Validate the workspace health block against its domain declaration."""
    from zicato.core.configuration import authored_dataclass_from_json  # noqa: PLC0415

    raw = {} if workspace_config is None else workspace_config.get("health", {})
    return authored_dataclass_from_json(HealthConfig, raw, path="health")


@dataclass(frozen=True, slots=True)
class EnvironmentBoundary:
    """An approved environment owner, its allowed functions, and the values it handles."""

    module: str
    functions: tuple[str, ...]
    variables: tuple[str, ...]
    role: str
    description: str


ENVIRONMENT_BOUNDARIES = (
    EnvironmentBoundary(
        "zicato/runtime/context.py",
        ("inherited_runtime_context", "bind_worker_runtime_context"),
        ("ZICATO_RUNTIME_CONTEXT",),
        "internal-handoff",
        "The isolated worker points nested children to its typed runtime context file. "
        "The coordinator never sets this pointer in its own environment.",
    ),
    EnvironmentBoundary(
        "zicato/_tournament_worker.py",
        ("_run_with_imports",),
        ("ZICATO_RUN_SCRATCH_DIR",),
        "harness-contract",
        "Compatibility scratch directory for targets that do not consume the typed RunContext.",
    ),
    EnvironmentBoundary(
        "zicato/models_config.py",
        ("RoleSpec.to_public_dict", "build_adk_model"),
        ("<models.engines.<name>.api_key_env>",),
        "secrets-boundary",
        "Read a named credential variable for model construction or availability reporting. "
        "Availability reports contain a boolean, never the credential.",
    ),
    EnvironmentBoundary(
        "zicato/integrations/goldfive.py",
        ("build_runtime_config",),
        ("<scoring.goldfive credential references>",),
        "secrets-boundary",
        "Resolve credential references declared by the typed evaluation integration.",
    ),
    EnvironmentBoundary(
        "zicato/check/validators.py",
        ("model_roles", "goldfive_integration"),
        ("<configured credential variables>",),
        "secrets-boundary",
        "Check that declared credentials are available before work starts; do not publish values.",
    ),
    EnvironmentBoundary(
        "zicato/tournament/worker_transport.py",
        ("scrubbed_worker_env",),
        (
            "<standard process context>",
            "<configured credential variables>",
            "<runtime.worker_env_passthrough>",
        ),
        "worker-environment",
        "Construct the worker environment from the explicit process allowlist, named credentials, "
        "and the configured target passthrough names.",
    ),
    EnvironmentBoundary(
        "zicato/runtime/spawn_permit.py",
        ("permit_dir",),
        ("XDG_RUNTIME_DIR",),
        "operating-system",
        "Locate the operating system's per-user runtime directory for worker permits.",
    ),
    EnvironmentBoundary(
        "zicato/tui/app.py",
        ("degrade_to_ascii",),
        ("LC_ALL", "LANG"),
        "operating-system",
        "Read the terminal locale to select characters the terminal can display.",
    ),
)


@dataclass(frozen=True, slots=True)
class EnvVarInfo:
    """One inspected variable and the approved boundary that consumes it."""

    name: str
    role: str
    description: str


def describe_env_vars() -> tuple[EnvVarInfo, ...]:
    """Derive the environment report from the mechanically checked boundary declarations."""
    return tuple(
        EnvVarInfo(variable, boundary.role, boundary.description)
        for boundary in ENVIRONMENT_BOUNDARIES
        for variable in boundary.variables
    )


# ---------------------------------------------------------------------------
# The loader
# ---------------------------------------------------------------------------


def load_config(*, overrides: Mapping[str, Any] | None = None) -> ZicatoConfig:
    """Resolve an explicit overlay over the declared defaults."""
    return resolve_configuration({}, overlay=InvocationOverlay.from_mapping(overrides or {})).values


__all__ = [
    "HealthConfig",
    "AuxConfig",
    "IntegrationConfig",
    "DashboardConfig",
    "RuntimeTuningConfig",
    "ZicatoConfig",
    "InvocationOverlay",
    "ResolvedConfiguration",
    "resolve_configuration",
    "EnvVarInfo",
    "load_config",
    "health_config_from_workspace",
    "describe_env_vars",
]
