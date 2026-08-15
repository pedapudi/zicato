# ruff: noqa: PLC0415
"""Named model engines, role inheritance, and secret-safe resolution."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from zicato.core.types import CallLLM

log = logging.getLogger("zicato.models_config")

MODEL_ROLES: tuple[str, ...] = (
    "harness",
    "auxiliary",
    "builder",
    "judge",
    "adjudicator",
    "user_emulator",
    "proposer_breadth",
    "proposer_depth",
)

PUBLIC_MODEL_ROLES: tuple[str, ...] = (
    "target",
    "evaluation",
    "builder",
    "judge",
    "adjudicator",
    "user_emulator",
    "proposer",
    "proposer_generate",
    "proposer_review",
)

_PUBLIC_TO_INTERNAL = {"target": "harness", "evaluation": "auxiliary"}
_DEFAULT_ENGINE = {
    "target": "target",
    "evaluation": "evaluation",
    "builder": "evaluation",
    "judge": "evaluation",
    "adjudicator": "evaluation",
    "user_emulator": "evaluation",
    "proposer": "evaluation",
    "proposer_generate": "proposer",
    "proposer_review": "proposer",
}


@dataclass(frozen=True, slots=True)
class RoleSpec:
    """One engine: an importable callable or a model plus transport."""

    call_llm: str | None = None
    model: str | None = None
    endpoint: str | None = None
    api_key_env: str | None = None
    revision: str | None = None

    @property
    def is_empty(self) -> bool:
        """``True`` iff neither a dotted path nor a model string is set."""
        return not self.call_llm and not self.model

    @property
    def uses_call_llm(self) -> bool:
        """``True`` iff this spec uses the dotted-path form."""
        return bool(self.call_llm)

    def to_dict(self) -> dict[str, Any]:
        """Serialize without resolving credential values."""
        if self.uses_call_llm:
            return {
                "call_llm": self.call_llm,
                **({"revision": self.revision} if self.revision else {}),
            }
        if self.model:
            return {
                "model": self.model,
                "endpoint": self.endpoint,
                "api_key_env": self.api_key_env,
                **({"revision": self.revision} if self.revision else {}),
            }
        return {}

    def to_public_dict(self) -> dict[str, Any]:
        """Add credential availability, never its value."""
        out = self.to_dict()
        if not self.uses_call_llm and self.model:
            env_name = self.api_key_env
            out["api_key_env_set"] = bool(env_name) and bool(os.environ.get(env_name or ""))
        return out

    def to_worker_spec(self) -> dict[str, Any]:
        """Secret-free subprocess representation."""
        return self.to_dict()


def role_spec_from_dict(raw: Any) -> RoleSpec:
    """Parse and strictly validate one engine."""
    if not isinstance(raw, Mapping):
        raise ValueError("model engine must be an object")
    unknown = set(raw) - {"call_llm", "model", "endpoint", "api_key_env", "revision"}
    if unknown:
        raise ValueError(f"unknown model engine keys: {sorted(unknown)}")
    call_llm = _opt_str(raw.get("call_llm"))
    model = _opt_str(raw.get("model"))
    if call_llm and model:
        raise ValueError("model engine must set exactly one of call_llm or model")
    if call_llm:
        if raw.get("endpoint") is not None or raw.get("api_key_env") is not None:
            raise ValueError("call_llm cannot be combined with endpoint or api_key_env")
        return RoleSpec(call_llm=call_llm, revision=_opt_str(raw.get("revision")))
    endpoint = _opt_str(raw.get("endpoint"))
    api_key_env = _opt_str(raw.get("api_key_env"))
    if not model and (endpoint or api_key_env):
        raise ValueError("endpoint and api_key_env require model")
    if not model:
        raise ValueError("model engine must set exactly one of call_llm or model")
    return RoleSpec(
        model=model,
        endpoint=endpoint,
        api_key_env=api_key_env,
        revision=_opt_str(raw.get("revision")),
    )


@dataclass(frozen=True, slots=True)
class ModelsConfig:
    """Resolved role specs plus their named-engine source."""

    harness: RoleSpec = RoleSpec()
    auxiliary: RoleSpec = RoleSpec()
    builder: RoleSpec = RoleSpec()
    judge: RoleSpec = RoleSpec()
    adjudicator: RoleSpec = RoleSpec()
    proposer_breadth: RoleSpec = RoleSpec()
    proposer_depth: RoleSpec = RoleSpec()
    user_emulator: RoleSpec = RoleSpec()
    proposer: RoleSpec = RoleSpec()
    engines: tuple[tuple[str, RoleSpec], ...] = ()
    assignments: tuple[tuple[str, str], ...] = ()
    guide: Any = None
    named: bool = False

    def role(self, name: str) -> RoleSpec:
        """Return the :class:`RoleSpec` for ``name`` (one of :data:`MODEL_ROLES`)."""
        if name not in MODEL_ROLES:
            raise ValueError(f"unknown model role {name!r}; expected one of {MODEL_ROLES}")
        return getattr(self, name)  # type: ignore[no-any-return]

    def to_dict(self) -> dict[str, Any]:
        """Serialize the configured schema."""
        if self.named:
            out = {
                "engines": {name: spec.to_dict() for name, spec in self.engines},
                "roles": dict(self.assignments),
            }
            if self.guide is not None:
                out["_guide"] = self.guide
            return out
        if self.guide is not None:
            return {"engines": {}, "roles": {}, "_guide": self.guide}
        legacy: dict[str, Any] = {}
        for name in MODEL_ROLES:
            spec = self.role(name)
            if not spec.is_empty:
                legacy[name] = spec.to_dict()
        return legacy

    def to_public_dict(self) -> dict[str, Any]:
        """Serialize engine definitions and effective role provenance."""
        if self.named:
            return {
                "engines": {name: spec.to_public_dict() for name, spec in self.engines},
                "roles": dict(self.assignments),
                "_guide": self.guide,
                "effective": {
                    role: {
                        "engine": self.engine_for(role),
                        "inherited": role not in dict(self.assignments),
                        "source": (
                            role
                            if role in dict(self.assignments)
                            else (
                                "proposer"
                                if role in {"proposer_generate", "proposer_review"}
                                and "proposer" in dict(self.assignments)
                                else "evaluation"
                            )
                        ),
                    }
                    for role in PUBLIC_MODEL_ROLES
                },
            }
        return {name: self.role(name).to_public_dict() for name in MODEL_ROLES}

    def engine_for(self, public_role: str) -> str | None:
        """Return the named engine selected by a public role."""
        if public_role not in PUBLIC_MODEL_ROLES:
            raise ValueError(f"unknown model role {public_role!r}")
        assigned = dict(self.assignments)
        name = assigned.get(public_role)
        if name is None and public_role in {"proposer_generate", "proposer_review"}:
            name = assigned.get("proposer")
        name = name or _DEFAULT_ENGINE[public_role]
        if name == "proposer":
            name = assigned.get("proposer", "evaluation")
        return name if name in dict(self.engines) else None


def models_config_from_dict(raw: Any) -> ModelsConfig:
    """Parse the named-engine models schema."""
    if raw is None:
        return ModelsConfig()
    if not isinstance(raw, Mapping):
        raise ValueError("models must be an object")
    if "engines" in raw or "roles" in raw:
        unknown = set(raw) - {"engines", "roles", "_guide"}
        if unknown:
            raise ValueError(f"unknown models keys: {sorted(unknown)}")
        engines_raw = raw.get("engines", {})
        roles_raw = raw.get("roles", {})
        if not isinstance(engines_raw, Mapping) or not isinstance(roles_raw, Mapping):
            raise ValueError("models.engines and models.roles must be objects")
        engines: dict[str, RoleSpec] = {}
        for name, value in engines_raw.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("engine names must be non-empty strings")
            engines[name] = role_spec_from_dict(value)
        assignments: dict[str, str] = {}
        for role, engine in roles_raw.items():
            if role not in PUBLIC_MODEL_ROLES:
                raise ValueError(f"unknown model role {role!r}")
            if not isinstance(engine, str) or engine not in engines:
                raise ValueError(f"models.roles.{role} refers to unknown engine {engine!r}")
            assignments[role] = engine

        def selected_name(public_role: str) -> str:
            name = assignments.get(public_role)
            if name is None and public_role in {"proposer_generate", "proposer_review"}:
                name = assignments.get("proposer")
            name = name or _DEFAULT_ENGINE[public_role]
            if name == "proposer":
                name = assignments.get("proposer", "evaluation")
            return name

        def selected(public_role: str) -> RoleSpec:
            name = selected_name(public_role)
            return engines.get(name, RoleSpec())

        target = selected("target")
        for isolated_role in ("evaluation", "user_emulator"):
            if not target.is_empty and selected_name("target") == selected_name(isolated_role):
                raise ValueError(
                    f"models.roles.{isolated_role} must not use the target engine; "
                    "evaluated and evaluator-side engines must be distinct"
                )

        return ModelsConfig(
            harness=target,
            auxiliary=selected("evaluation"),
            builder=selected("builder"),
            judge=selected("judge"),
            adjudicator=selected("adjudicator"),
            user_emulator=selected("user_emulator"),
            proposer=selected("proposer"),
            proposer_breadth=selected("proposer_generate"),
            proposer_depth=selected("proposer_review"),
            engines=tuple(engines.items()),
            assignments=tuple(assignments.items()),
            guide=raw.get("_guide"),
            named=True,
        )
    raise ValueError(
        "direct models.<role> configuration is no longer supported; move each "
        "connection under models.engines and map public names in models.roles "
        "(see docs/design/MODEL-CONFIG.md)"
    )


def load_models_config(workspace_config: Mapping[str, Any]) -> ModelsConfig:
    return models_config_from_dict(workspace_config.get("models"))


def resolve_text_call_llm(spec: RoleSpec, *, role: str) -> CallLLM:
    """Resolve an engine to the text-call seam."""
    if spec.uses_call_llm:
        assert spec.call_llm is not None  # narrowed by uses_call_llm
        return _import_call_llm(spec.call_llm, role=role)
    if not spec.model:
        raise ValueError(f"models.{role}: neither a call_llm dotted path nor a model string is set")
    return _resolve_model_spec_call_llm(spec, role=role)


def _import_call_llm(dotted: str, *, role: str) -> CallLLM:
    """Import + validate a dotted-path callable for a text-call_llm role."""
    from zicato.import_path import import_dotted_path

    result: Any = import_dotted_path(dotted, label=f"models.{role}.call_llm")
    if not callable(result):
        raise ValueError(
            f"models.{role}.call_llm: {dotted!r} resolved to "
            f"{type(result).__name__}, expected a callable"
        )
    return result  # type: ignore[no-any-return]


def build_adk_model(spec: RoleSpec, *, role: str) -> Any:
    """Build a native model object; read credentials only here."""
    if not spec.model:
        raise ValueError(f"models.{role}: a model string is required to build an ADK model")
    if not spec.endpoint and not spec.api_key_env:
        return spec.model
    try:
        from google.adk.models.lite_llm import LiteLlm
    except ImportError as exc:
        raise ValueError(
            f"models.{role}: building a LiteLlm needs the 'litellm' package, "
            "which the 'adk' extra supplies via google-adk[extensions]; run "
            "`uv sync --all-extras` (or reinstall zicato with the adk extra) "
            "to pull it in. Alternatively use the call_llm dotted-path form "
            "instead of an endpoint model spec."
        ) from exc
    kwargs: dict[str, Any] = {"model": spec.model}
    if spec.endpoint:
        kwargs["api_base"] = spec.endpoint
    if spec.api_key_env:
        key = os.environ.get(spec.api_key_env)
        if not key:
            raise ValueError(
                f"models.{role}: credential environment variable "
                f"{spec.api_key_env!r} is not set"
            )
        kwargs["api_key"] = key
    return LiteLlm(**kwargs)


def _resolve_model_spec_call_llm(spec: RoleSpec, *, role: str) -> CallLLM:
    """Adapt a native model to the text-call seam."""
    model = build_adk_model(spec, role=role)
    try:
        from goldfive._llm_detect import make_default_adk_call_llm
    except ImportError as exc:
        raise ValueError(
            f"models.{role}: building a call_llm from a model spec needs the "
            "optional 'adk' extra (goldfive's ADK detector); use the call_llm "
            "dotted-path form instead if ADK is unavailable"
        ) from exc
    call_llm = make_default_adk_call_llm(model)
    if call_llm is None:
        raise ValueError(
            f"models.{role}: could not build a call_llm from model "
            f"{spec.model!r} (ADK could not resolve it to a model); check the "
            "model id / endpoint, or use the call_llm dotted-path form"
        )
    return call_llm


class RoleResolutionError(ValueError):
    """Deferred engine resolution failed at first call."""


_DEFERRED_ROLE_FAILURES: dict[str, str] = {}


def deferred_role_failures() -> dict[str, str]:
    """Return failures that the worker must classify as infrastructure."""
    return dict(_DEFERRED_ROLE_FAILURES)


def clear_deferred_role_failures() -> None:
    """Reset the register. For tests; a worker process resolves once."""
    _DEFERRED_ROLE_FAILURES.clear()


def lazy_text_call_llm(spec: RoleSpec, *, role: str) -> CallLLM:
    """Resolve native engines on first call and register resolution failure."""
    if spec.uses_call_llm:
        assert spec.call_llm is not None  # narrowed by uses_call_llm
        return _import_call_llm(spec.call_llm, role=role)
    if not spec.model:
        raise ValueError(f"models.{role}: neither a call_llm dotted path nor a model string is set")

    # One-slot cache: the underlying call_llm is built once, on the first
    # call, and reused for every later call of this role. A list (not a
    # ``nonlocal``) keeps the closure trivially readable.
    resolved: list[CallLLM] = []

    async def _lazy_call_llm(system: str, user: str, model: str) -> str:
        if not resolved:
            try:
                resolved.append(_resolve_model_spec_call_llm(spec, role=role))
            except Exception as exc:
                # Record BEFORE raising: the caller may be a judge, and every
                # judge boundary swallows. See ``deferred_role_failures``.
                _DEFERRED_ROLE_FAILURES.setdefault(role, str(exc))
                log.error(
                    "models.%s: deferred resolution of model %r failed at its first "
                    "call (%s); this board unit will be reported as a failed run",
                    role,
                    spec.model,
                    exc,
                )
                raise RoleResolutionError(str(exc)) from exc
        return await resolved[0](system, user, model)

    return _lazy_call_llm


def resolve_builder_model(spec: RoleSpec, *, role: str = "builder") -> Any:
    """Resolve the builder engine to a native model or custom callable."""
    if spec.uses_call_llm:
        assert spec.call_llm is not None
        from zicato.import_path import import_dotted_path

        return import_dotted_path(spec.call_llm, label=f"models.{role}.call_llm")
    if not spec.model:
        raise ValueError(f"models.{role}: neither a call_llm dotted path nor a model string is set")
    return build_adk_model(spec, role=role)


def _opt_str(value: Any) -> str | None:
    """Coerce an optional JSON value into ``str | None`` (blank ⇒ ``None``)."""
    if value is None:
        return None
    text = str(value)
    return text or None


__all__ = [
    "MODEL_ROLES",
    "RoleSpec",
    "ModelsConfig",
    "role_spec_from_dict",
    "models_config_from_dict",
    "load_models_config",
    "resolve_text_call_llm",
    "lazy_text_call_llm",
    "RoleResolutionError",
    "deferred_role_failures",
    "clear_deferred_role_failures",
    "resolve_builder_model",
    "build_adk_model",
]
