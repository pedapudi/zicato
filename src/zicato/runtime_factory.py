"""Resolve workspace model roles and runtime controls."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields
from pathlib import Path
from typing import Any

from zicato.config import ResolvedConfiguration, resolve_configuration
from zicato.core.adapter_config import DriverImportContext
from zicato.core.configuration import authored_dataclass_from_json
from zicato.core.runtime_context import TelemetryEndpoints
from zicato.core.settings import RuntimeDeclaration, RuntimeSettings
from zicato.core.types import CallLLM, RuntimeConfig
from zicato.core.workspace import assert_distinct_callables
from zicato.driver_imports import with_workspace_imports
from zicato.import_path import import_dotted_path
from zicato.models_config import load_models_config, resolve_text_call_llm
from zicato.runtime.effective_settings import (
    SOURCE_HOST_CPU_COUNT,
)


def resolve_parallelism(
    runtime_dict: Mapping[str, Any], *, configuration: ResolvedConfiguration | None = None
) -> tuple[int, str]:
    """Return concurrency and its invocation-local source."""
    resolved = configuration or resolve_configuration({"runtime": runtime_dict})
    return resolved.values.runtime.parallelism, resolved.sources["runtime.parallelism"]


def resolve_host_worker_permits(
    runtime_dict: Mapping[str, Any], *, configuration: ResolvedConfiguration | None = None
) -> tuple[int | None, str]:
    """Return the declared host ceiling; an automatic ceiling names the host."""
    resolved = configuration or resolve_configuration({"runtime": runtime_dict})
    value = resolved.values.runtime.host_worker_permits
    source = (
        SOURCE_HOST_CPU_COUNT if value is None else resolved.sources["runtime.host_worker_permits"]
    )
    return value, source


@with_workspace_imports
def resolve_role_call_llm(
    workspace_config: Mapping[str, Any], *, role: str, workspace_root: Path | None = None
) -> CallLLM:
    """Resolve one model role to the callable a round runs it on.

    Two sources, in order: the ``models`` block's engine for ``role``,
    then the ``runtime.<role>_call_llm`` dotted path. A workspace that
    configures neither has not said what the role runs on, and this
    raises naming both keys — the message an operator acts on, so it
    names the file's keys rather than a function argument.

    Shared by :func:`make_runtime_config` and by ``zicato evolve``, which
    resolves the two roles a round always needs before opening one so the
    refusal arrives at the command line rather than mid-loop.
    """
    models = load_models_config(workspace_config)
    spec = models.role(role)
    if not spec.is_empty:
        return resolve_text_call_llm(spec, role=role)
    runtime_dict = workspace_config.get("runtime", {}) or {}
    dotted = runtime_dict.get(f"{role}_call_llm") if isinstance(runtime_dict, Mapping) else None
    if not dotted:
        raise ValueError(
            f"model role {role!r} is unconfigured: name an engine for it under "
            f"models.engines / models.roles, or give runtime.{role}_call_llm a "
            f"dotted import path"
        )
    return _import_callable(str(dotted), kind=f"{role}_call_llm")


@with_workspace_imports
def make_runtime_config(
    workspace_config: Mapping[str, Any],
    *,
    workspace_root: Path | None = None,
    target_call_llm: CallLLM | None = None,
    evaluation_call_llm: CallLLM | None = None,
    configuration: ResolvedConfiguration | None = None,
    telemetry: TelemetryEndpoints | None = None,
    execution_roles: bytes | None = None,
) -> RuntimeConfig:
    """Assemble a :class:`RuntimeConfig` from workspace config + optional overrides.

    Parameters
    ----------
    workspace_config:
        Dict produced by :func:`zicato.workspace_loader.load_workspace_config`.
        Read fields:

        * ``runtime.instance_id`` (string; defaults to ``"default"``).
        * ``runtime.workspace_root`` (path; overridden by the explicit
          ``workspace_root`` kwarg when supplied).
        * ``runtime.target_call_llm`` (dotted path; only consulted
          when the kwarg is ``None`` and ``models`` names no engine for
          the role — see :func:`resolve_role_call_llm`).
        * ``runtime.evaluation_call_llm`` (dotted path; same rule).
        * ``runtime.seed`` (int or null).
    workspace_root:
        Optional override for the workspace root path. When ``None``
        we fall back to ``config['runtime']['workspace_root']`` and then
        to ``.zicato`` (relative to the operator's cwd).
    target_call_llm, evaluation_call_llm:
        Optional pre-resolved callables. Each one bypasses the config's
        dotted-path lookup when supplied.

    Returns
    -------
    RuntimeConfig

    Raises
    ------
    ValueError
        Missing dotted paths when no callable kwarg was supplied;
        non-string dotted paths; or
        :func:`assert_distinct_callables` rejecting the pair.
    """
    resolved = configuration or resolve_configuration(workspace_config)
    settings = resolved.values.runtime

    resolved_root: Path
    if workspace_root is not None:
        resolved_root = Path(workspace_root)
    else:
        declaration = authored_dataclass_from_json(
            RuntimeDeclaration, workspace_config.get("runtime", {}), path="config.runtime"
        )
        resolved_root = Path(declaration.workspace_root)

    from zicato.models_config import (  # noqa: PLC0415
        build_adk_model,
        capture_execution_roles,
        execution_roles_from_json,
        resolve_worker_role,
        role_spec_from_dict,
    )

    captured = (
        execution_roles
        if execution_roles is not None
        else capture_execution_roles(workspace_config)
    )
    roles = execution_roles_from_json(captured)

    def resolved_role(role: str) -> CallLLM | None:
        document = roles.get(role)
        if role not in {"target", "evaluation"} and document == roles.get("evaluation"):
            return None
        return resolve_worker_role(document, role=role) if document is not None else None

    target = target_call_llm or resolved_role("target")
    aux = evaluation_call_llm or resolved_role("evaluation")
    if target is None or aux is None:
        missing_role = "target" if target is None else "evaluation"
        raise ValueError(
            f"model role {missing_role!r} is unconfigured: "
            f"set runtime.{missing_role}_call_llm or models.roles.{missing_role}"
        )
    judge = resolved_role("judge")
    adjudicator = resolved_role("adjudicator")
    user_emulator = resolved_role("user_emulator")
    proposer = resolved_role("proposer")
    proposer_breadth = resolved_role("proposer_breadth")
    proposer_depth = resolved_role("proposer_depth")

    def model_name(role: str) -> str | None:
        value = roles.get(role, {}).get("models_role", {}).get("model")
        return value if isinstance(value, str) else None

    target_model: Any = None
    target_document = roles.get("target", {})
    if model_name("target") and target_call_llm is None:
        target_model = build_adk_model(
            role_spec_from_dict(target_document["models_role"]),
            role="target",
            transport=target_document.get("transport"),
        )
        if isinstance(target_model, str):
            target_model = None

    # Defense in depth — also re-checked by the runner.
    assert_distinct_callables(target, aux)

    return RuntimeConfig(
        **{item.name: getattr(settings, item.name) for item in fields(RuntimeSettings)},
        configuration=resolved,
        execution_roles=captured,
        telemetry=telemetry or TelemetryEndpoints(),
        workspace_root=resolved_root,
        driver_imports=DriverImportContext.from_config(workspace_config, resolved_root),
        target_call_llm=target,
        evaluation_call_llm=aux,
        judge_call_llm=judge,
        adjudicator_call_llm=adjudicator,
        user_emulator_call_llm=user_emulator,
        proposer_call_llm=proposer,
        proposer_breadth_call_llm=proposer_breadth,
        proposer_depth_call_llm=proposer_depth,
        proposer_breadth_model=model_name("proposer_breadth"),
        proposer_depth_model=model_name("proposer_depth"),
        proposer_model=model_name("proposer"),
        target_model=target_model,
    )


def _import_callable(dotted: str, *, kind: str) -> CallLLM:
    """Resolve a ``pkg.mod:attr`` or ``pkg.mod.attr`` dotted path to a callable.

    Delegates to :func:`zicato.import_path.import_dotted_path` so both the
    colon-separated (entry-point style) and dot-separated forms are handled
    identically by the single shared implementation.
    """
    result: Any = import_dotted_path(dotted, label=kind)
    if not callable(result):
        raise ValueError(
            f"{kind}: {dotted!r} resolved to {type(result).__name__}, " "expected a callable"
        )
    # mypy can't narrow Any → CallLLM here, but the runner re-checks
    # the call shape on its first invocation.
    return result  # type: ignore[no-any-return]


__all__ = [
    "make_runtime_config",
    "resolve_host_worker_permits",
    "resolve_parallelism",
    "resolve_role_call_llm",
]
