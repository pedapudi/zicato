# ruff: noqa: PLC0415
"""Named model engines, role inheritance, and secret-safe resolution."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zicato.core.configuration import ConfigurationError, authored_dataclass_from_json
from zicato.core.types import CallLLM

log = logging.getLogger("zicato.models_config")

MODEL_ROLES: tuple[str, ...] = (
    "target",
    "evaluation",
    "judge",
    "adjudicator",
    "user_emulator",
    "proposer_breadth",
    "proposer_depth",
)

PUBLIC_MODEL_ROLES: tuple[str, ...] = (
    "target",
    "evaluation",
    "judge",
    "adjudicator",
    "user_emulator",
    "proposer",
    "proposer_generate",
    "proposer_review",
)

_DEFAULT_ENGINE = {
    "target": "target",
    "evaluation": "evaluation",
    "judge": "evaluation",
    "adjudicator": "evaluation",
    "user_emulator": "evaluation",
    "proposer": "evaluation",
    "proposer_generate": "proposer",
    "proposer_review": "proposer",
}


@dataclass(frozen=True, slots=True)
class RoleSpec:
    """One engine: an importable callable or a model plus transport.

    Fields
    ------
    call_llm:
        Import path of a callable accepting system text, user text, and model name.
    model:
        Model name understood by the connection endpoint.
    endpoint:
        Connection URL. Null uses the model runtime's default endpoint.
    api_key_env:
        Name of the environment variable holding the credential, never its value.
    revision:
        Logical deployment label recorded with this engine.
    """

    call_llm: str | None = None
    model: str | None = None
    endpoint: str | None = None
    api_key_env: str | None = field(default=None, metadata={"secret_reference": True})
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
    """Validate one authored engine before resolving its connection."""
    spec = authored_dataclass_from_json(RoleSpec, raw, path="model engine")
    _validate_engine(spec)
    return spec


def _validate_engine(spec: RoleSpec) -> None:
    if spec.is_empty and (spec.endpoint or spec.api_key_env):
        raise ValueError("endpoint and api_key_env require model")
    if bool(spec.call_llm) == bool(spec.model):
        raise ValueError("model engine must set exactly one of call_llm or model")
    if spec.call_llm and (spec.endpoint is not None or spec.api_key_env is not None):
        raise ValueError("call_llm cannot be combined with endpoint or api_key_env")


@dataclass(frozen=True, slots=True)
class ModelDeclarations:
    """Named engines and the public roles assigned to them.

    Fields
    ------
    engines:
        Reusable connections keyed by operator-chosen engine names.
    roles:
        Public role names mapped to engine names. Omitted roles inherit their
        declared evaluation or proposer default.
    guide:
        Optional explanatory JSON stored under _guide; never used during execution.
    """

    engines: Mapping[str, RoleSpec] = field(default_factory=dict)
    roles: Mapping[str, str] = field(default_factory=dict)
    guide: Any = field(default=None, metadata={"persisted_name": "_guide"})

    def __post_init__(self) -> None:
        for name, spec in self.engines.items():
            if not name.strip():
                raise ConfigurationError(
                    "models.engines", "value", "engine names must be non-empty"
                )
            try:
                _validate_engine(spec)
            except ValueError as exc:
                raise ConfigurationError(f"models.engines.{name}", "value", str(exc)) from exc
        for role, engine in self.roles.items():
            if role not in PUBLIC_MODEL_ROLES:
                raise ConfigurationError(
                    f"models.roles.{role}", "unknown", f"expected one of {PUBLIC_MODEL_ROLES}"
                )
            if engine not in self.engines:
                raise ConfigurationError(
                    f"models.roles.{role}", "value", f"refers to unknown engine {engine!r}"
                )
        target = self.selected_name("target")
        if target in self.engines:
            for role in PUBLIC_MODEL_ROLES[1:]:
                if self.selected_name(role) == target:
                    raise ConfigurationError(
                        f"models.roles.{role}",
                        "value",
                        "must not use the target engine; evaluated and evaluator-side "
                        "engines must be distinct",
                    )

    def selected_name(self, public_role: str) -> str:
        """Apply the role inheritance declared for named engines."""
        name = self.roles.get(public_role)
        if name is None and public_role in {"proposer_generate", "proposer_review"}:
            name = self.roles.get("proposer")
        name = name or _DEFAULT_ENGINE[public_role]
        return self.roles.get("proposer", "evaluation") if name == "proposer" else name


@dataclass(frozen=True, slots=True)
class ModelsConfig:
    """Resolved role specs plus their named-engine source."""

    target: RoleSpec = RoleSpec()
    evaluation: RoleSpec = RoleSpec()
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
    if not raw or "engines" in raw or "roles" in raw or "_guide" in raw:
        declared = authored_dataclass_from_json(ModelDeclarations, raw, path="models")
        engines = dict(declared.engines)
        assignments = dict(declared.roles)

        def selected(public_role: str) -> RoleSpec:
            return engines.get(declared.selected_name(public_role), RoleSpec())

        return ModelsConfig(
            target=selected("target"),
            evaluation=selected("evaluation"),
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


def capture_execution_roles(workspace_config: Mapping[str, Any]) -> bytes:
    """Capture effective role inheritance and nonsecret worker transport settings."""
    models = load_models_config(workspace_config)
    runtime = workspace_config.get("runtime") or {}
    roles = {}
    captured: dict[RoleSpec, dict[str, Any]] = {}
    for role in (*MODEL_ROLES, "proposer"):
        spec = getattr(models, role)
        if spec.is_empty:
            dotted = runtime.get(f"{role}_call_llm")
            if dotted is None and role not in {"target", "evaluation"}:
                dotted = runtime.get("evaluation_call_llm")
            if not dotted:
                continue
            spec = RoleSpec(call_llm=dotted)
        if spec not in captured:
            document = {"models_role": spec.to_worker_spec()}
            if spec.model and not spec.endpoint and not spec.api_key_env:
                document["transport"] = _capture_native_transport(spec, role=role)
            captured[spec] = document
        roles[role] = captured[spec]
    return json.dumps(roles, sort_keys=True, separators=(",", ":")).encode()


def execution_roles_from_json(raw: bytes) -> dict[str, Any]:
    """Validate captured role documents before hashing or worker reconstruction."""
    roles = json.loads(raw)
    allowed = set(MODEL_ROLES) | {"proposer"}
    if not isinstance(roles, dict) or set(roles) - allowed:
        raise ValueError("captured execution roles must be an object of configured role names")
    for document in roles.values():
        _captured_role_spec(document)
    return roles


def _captured_role_spec(document: Any) -> RoleSpec:
    """Validate one captured role, including its native credential reference."""
    if not isinstance(document, dict) or set(document) - {"models_role", "transport"}:
        raise ValueError("invalid captured worker role")
    spec = role_spec_from_dict(document.get("models_role"))
    if "transport" not in document:
        return spec
    transport = document["transport"]
    required = {"model_factory", "client_factory", "base_url", "api_version"}
    optional = {"project", "location", "api_key_env", "credential_file"}
    if (
        not spec.model
        or spec.endpoint
        or spec.api_key_env
        or not isinstance(transport, dict)
        or set(transport) != required | optional | {"backend"}
        or type(transport["backend"]) is not bool
        or any(not isinstance(transport[name], str) or not transport[name] for name in required)
        or any(
            transport[name] is not None
            and (not isinstance(transport[name], str) or not transport[name])
            for name in optional
        )
        or bool(transport["api_key_env"])
        and bool(transport["credential_file"])
    ):
        raise ValueError("invalid captured native transport")
    return spec


def execution_roles_for_runtime(config: Any) -> bytes:
    """Describe the actual callables, retaining declared revisions when they agree."""
    from zicato.import_path import _callable_dotted_path

    declared = (
        execution_roles_from_json(config.execution_roles)
        if config.execution_roles is not None
        else {}
    )
    roles = {}
    for role in (*MODEL_ROLES, "proposer"):
        fn = getattr(config, f"{role}_call_llm", None)
        if fn is None:
            fn = config.evaluation_call_llm
        captured = getattr(fn, "__zicato_worker_role__", None)
        if captured is not None:
            roles[role] = json.loads(captured)
            continue
        document = declared.get(role, {})
        dotted = document.get("models_role", {}).get("call_llm")
        if dotted and _import_call_llm(dotted, role=role) is fn:
            roles[role] = document
        else:
            roles[role] = {"models_role": {"call_llm": _callable_dotted_path(fn)}}
    return json.dumps(roles, sort_keys=True, separators=(",", ":")).encode()


def resolve_worker_role(document: Mapping[str, Any], *, role: str, lazy: bool = False) -> CallLLM:
    """Reconstruct one captured worker role through the model configuration owner."""
    if "models_role" not in document and "dotted" in document:
        document = {"models_role": {"call_llm": document["dotted"]}}
    spec = _captured_role_spec(document)
    resolve = lazy_text_call_llm if lazy else resolve_text_call_llm
    return resolve(spec, role=role, transport=document.get("transport"))


def _capture_native_transport(spec: RoleSpec, *, role: str) -> dict[str, Any]:
    """Observe the installed native client's resolved settings without a request."""
    from google.adk.models.registry import LLMRegistry
    from google.auth import _cloud_sdk

    credential_file = os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or (
        _cloud_sdk.get_application_default_credentials_path()  # type: ignore[no-untyped-call]
    )
    client = None
    try:
        assert spec.model is not None
        model: Any = LLMRegistry.new_llm(spec.model)
        client = model.api_client
        resolved = client._api_client
        key = resolved.api_key
        key_env = next(
            (
                name
                for name in ("GOOGLE_API_KEY", "GEMINI_API_KEY")
                if key and os.getenv(name) == key
            ),
            None,
        )
        if key_env:
            credential_file = None
        else:
            if not Path(credential_file).is_file():
                credential_file = None
            if not resolved.project or not resolved.location:
                raise ValueError("native execution requires a resolved project and location")
        result = {
            "model_factory": f"{type(model).__module__}:{type(model).__qualname__}",
            "client_factory": f"{type(client).__module__}:{type(client).__qualname__}",
            "backend": bool(client.vertexai),
            "project": resolved.project if client.vertexai else None,
            "location": resolved.location if client.vertexai else None,
            "base_url": resolved._http_options.base_url,
            "api_version": resolved._http_options.api_version,
            "api_key_env": key_env,
            "credential_file": credential_file,
        }
        return result
    except (AttributeError, TypeError, ValueError) as exc:
        raise ConfigurationError(f"models.{role}", "value", str(exc)) from exc
    finally:
        if client is not None:
            client.close()


def _build_captured_native_model(spec: RoleSpec, transport: Mapping[str, Any]) -> Any:
    """Reconstruct a native client through its public constructor and model hook."""
    from functools import cached_property

    from zicato.import_path import import_dotted_path

    model_type = import_dotted_path(transport["model_factory"], label="captured model factory")
    client_type = import_dotted_path(transport["client_factory"], label="captured client factory")
    key_env = transport["api_key_env"]
    key = os.getenv(key_env) if key_env else None
    if key_env and not key:
        raise ConfigurationError(
            "models", "value", f"credential environment variable {key_env!r} is not set"
        )
    credentials = None
    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
    if transport["credential_file"]:
        from google.auth import load_credentials_from_file

        credentials, _ = load_credentials_from_file(transport["credential_file"], scopes=scopes)  # type: ignore[no-untyped-call]
    elif key_env is None:
        from google.auth import default

        credentials, _ = default(scopes=scopes)

    class CapturedModel(model_type):  # type: ignore[misc,valid-type]
        @cached_property
        def api_client(self) -> Any:
            return client_type(
                vertexai=transport["backend"],
                project=transport["project"],
                location=transport["location"],
                api_key=key,
                credentials=credentials,
                http_options={
                    "base_url": transport["base_url"],
                    "api_version": transport["api_version"],
                },
            )

    return CapturedModel(model=spec.model)


def resolve_text_call_llm(
    spec: RoleSpec, *, role: str, transport: Mapping[str, Any] | None = None
) -> CallLLM:
    """Resolve an engine to the text-call seam."""
    if spec.uses_call_llm:
        assert spec.call_llm is not None  # narrowed by uses_call_llm
        return _import_call_llm(spec.call_llm, role=role)
    if not spec.model:
        raise ValueError(f"models.{role}: neither a call_llm dotted path nor a model string is set")
    return _resolve_model_spec_call_llm(spec, role=role, transport=transport)


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


def build_adk_model(
    spec: RoleSpec, *, role: str, transport: Mapping[str, Any] | None = None
) -> Any:
    """Build a native model object; read credentials only here."""
    if not spec.model:
        raise ValueError(f"models.{role}: a model string is required to build an ADK model")
    if transport is not None:
        return _build_captured_native_model(spec, transport)
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


def _resolve_model_spec_call_llm(
    spec: RoleSpec, *, role: str, transport: Mapping[str, Any] | None = None
) -> CallLLM:
    """Adapt a native model to the text-call seam."""
    model = build_adk_model(spec, role=role, transport=transport)
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
    document = {"models_role": spec.to_worker_spec()}
    if transport is not None:
        document["transport"] = dict(transport)
    call_llm.__zicato_worker_role__ = json.dumps(document, sort_keys=True).encode()  # type: ignore[attr-defined]
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


def lazy_text_call_llm(
    spec: RoleSpec, *, role: str, transport: Mapping[str, Any] | None = None
) -> CallLLM:
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
                resolved.append(_resolve_model_spec_call_llm(spec, role=role, transport=transport))
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
    "build_adk_model",
]
