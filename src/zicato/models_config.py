# ruff: noqa: PLC0415
# ADK / goldfive symbols are imported INSIDE the resolver functions on
# purpose — hoisting them to module scope would force the optional
# ``google-adk`` extra on every importer of this module. The dotted-path
# form (and the absent-``models`` default) must stay dependency-light; only
# the model-spec form pulls ADK in, and then with a clear, actionable error
# when the extra is missing. See :func:`_resolve_model_spec_call_llm`.
"""The unified ``models`` config — per-role LLM endpoint/model resolution.

A model/endpoint is **runtime infrastructure, not part of the evaluation
contract** (the contract pins the harness *identity* / entrypoint, not the
``call_llm`` backing it), so editing a role here does NOT roll the epoch —
unlike the board / scoring / tournament sections.

Schema
------
The workspace ``config.json`` may carry an optional ``models`` block::

    "models": {
      "harness":   { "call_llm": "pkg.mod:fn" },
      "auxiliary": { "model": "...", "endpoint": "...|null",
                     "api_key_env": "...|null" },
      "builder":   { ... },
      "judge":     { ... },
      "proposer_breadth": { ... },
      "proposer_depth":   { ... }
    }

Each role (**harness · auxiliary · builder · judge · proposer_breadth ·
proposer_depth**) is OPTIONAL and is EITHER:

* ``{"call_llm": "pkg.mod:fn"}`` — a dotted-path callable (today's
  behavior, fully backward-compatible), OR
* ``{"model": "...", "endpoint": "...|null", "api_key_env": "...|null"}`` —
  a model spec built into a callable / ADK model on demand.

An absent ``models`` block (or an absent role within it) ⇒ today's
resolution is unchanged: the caller falls back to the CLI kwargs /
``runtime.{harness,auxiliary}_call_llm`` / ``builder.json`` exactly as
before.

Secret safety
-------------
``api_key_env`` is the NAME of an environment variable; the value is read
from :data:`os.environ` ONLY at resolve time (inside
:func:`_resolve_model_spec_call_llm` / :func:`build_adk_model`). It is
never stored on the spec, serialized, logged, or returned to the frontend.
:func:`RoleSpec.to_public_dict` carries only the env-var name plus a
"is it set?" boolean — never the value.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from zicato.core.types import CallLLM

log = logging.getLogger("zicato.models_config")

#: The LLM roles the unified ``models`` block configures, in the order the
#: settings UI lists them. ``proposer_breadth`` / ``proposer_depth`` are the
#: WS-ENS ensemble roles: the best-of-N proposer's slate SAMPLING (breadth)
#: and its CRITIQUE + REVISE passes (depth). Both are OPTIONAL and, when
#: unconfigured, fall back to the auxiliary surface (byte-identical) — see
#: :meth:`zicato.core.runtime.RuntimeConfig.effective_proposer_breadth_call_llm`.
MODEL_ROLES: tuple[str, ...] = (
    "harness",
    "auxiliary",
    "builder",
    "judge",
    "proposer_breadth",
    "proposer_depth",
)


@dataclass(frozen=True, slots=True)
class RoleSpec:
    """How ONE role reaches a model — a dotted path XOR a model spec.

    Exactly one form is populated:

    * :attr:`call_llm` set ⇒ the dotted-path form (import the callable).
    * :attr:`model` set ⇒ the model-spec form (build a callable / ADK model
      from ``model`` + ``endpoint`` + ``os.environ[api_key_env]``).

    Both empty is a legal "unconfigured" spec (:meth:`is_empty`) — the role
    falls back to today's resolution.

    Secret safety: :attr:`api_key_env` is an environment-variable NAME, never
    a secret value. The value is read from :data:`os.environ` only at resolve
    time and is never stored on this dataclass.
    """

    call_llm: str | None = None
    model: str | None = None
    endpoint: str | None = None
    api_key_env: str | None = None

    @property
    def is_empty(self) -> bool:
        """``True`` iff neither a dotted path nor a model string is set."""
        return not self.call_llm and not self.model

    @property
    def uses_call_llm(self) -> bool:
        """``True`` iff this spec uses the dotted-path form."""
        return bool(self.call_llm)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the on-disk ``config.json`` shape (round-trips).

        Emits only the populated form's keys — the dotted-path form writes
        ``{"call_llm": ...}``; the model-spec form writes ``{"model": ...,
        "endpoint": ..., "api_key_env": ...}``. ``api_key_env`` is the env-var
        NAME (never a secret). An empty spec serializes to ``{}``.
        """
        if self.uses_call_llm:
            return {"call_llm": self.call_llm}
        if self.model:
            return {
                "model": self.model,
                "endpoint": self.endpoint,
                "api_key_env": self.api_key_env,
            }
        return {}

    def to_public_dict(self) -> dict[str, Any]:
        """Serialize for the frontend — NEVER includes a secret value.

        Carries the same shape :meth:`to_dict` does PLUS an
        ``api_key_env_set`` boolean for the model-spec form: whether the named
        env var is currently set (read here but the value never emitted), so
        the UI can render a "set / unset" indicator without ever seeing the
        secret. The dotted-path form omits the indicator.
        """
        out = self.to_dict()
        if not self.uses_call_llm and self.model:
            env_name = self.api_key_env
            out["api_key_env_set"] = bool(env_name) and bool(os.environ.get(env_name or ""))
        return out

    def to_worker_spec(self) -> dict[str, Any]:
        """Serialize for the subprocess worker's args file.

        The worker re-resolves the role with :func:`resolve_text_call_llm` in
        a fresh interpreter, so this carries the SAME secret-free shape
        :meth:`to_dict` does (the env-var NAME, read from the worker's own
        :data:`os.environ` at resolve time). Identical to :meth:`to_dict`;
        named separately so the worker contract is explicit at the call site.
        """
        return self.to_dict()


def role_spec_from_dict(raw: Any) -> RoleSpec:
    """Parse one role block of the ``models`` config into a :class:`RoleSpec`.

    Absent / non-mapping / empty ⇒ the empty spec (the role falls back to
    today's resolution). A ``call_llm`` key wins (the dotted-path form);
    otherwise ``model`` / ``endpoint`` / ``api_key_env`` build the model-spec
    form. Unknown keys are ignored so a forward-compatible file loads.
    """
    if not isinstance(raw, Mapping):
        return RoleSpec()
    call_llm = _opt_str(raw.get("call_llm"))
    if call_llm:
        return RoleSpec(call_llm=call_llm)
    return RoleSpec(
        model=_opt_str(raw.get("model")),
        endpoint=_opt_str(raw.get("endpoint")),
        api_key_env=_opt_str(raw.get("api_key_env")),
    )


@dataclass(frozen=True, slots=True)
class ModelsConfig:
    """The parsed ``models`` block — one :class:`RoleSpec` per role.

    Every role defaults to the empty spec, so an absent ``models`` block (or
    an absent role) leaves that role's resolution unchanged from today.
    """

    harness: RoleSpec = RoleSpec()
    auxiliary: RoleSpec = RoleSpec()
    builder: RoleSpec = RoleSpec()
    judge: RoleSpec = RoleSpec()
    proposer_breadth: RoleSpec = RoleSpec()
    proposer_depth: RoleSpec = RoleSpec()

    def role(self, name: str) -> RoleSpec:
        """Return the :class:`RoleSpec` for ``name`` (one of :data:`MODEL_ROLES`)."""
        if name not in MODEL_ROLES:
            raise ValueError(f"unknown model role {name!r}; expected one of {MODEL_ROLES}")
        return getattr(self, name)  # type: ignore[no-any-return]

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the on-disk ``models`` block (round-trips).

        Only non-empty roles are written, so a config whose roles are all
        unconfigured serializes to ``{}`` and reads back as all-default.
        """
        out: dict[str, Any] = {}
        for name in MODEL_ROLES:
            spec = self.role(name)
            if not spec.is_empty:
                out[name] = spec.to_dict()
        return out

    def to_public_dict(self) -> dict[str, Any]:
        """Serialize EVERY role for the frontend — never a secret value.

        Unlike :meth:`to_dict` (which omits empty roles for a clean on-disk
        form), this always emits all four roles so the settings UI can render
        an editable section for each, even the unconfigured ones. Secret-safe:
        a model-spec role carries ``api_key_env_set`` (a boolean), never the
        key value.
        """
        return {name: self.role(name).to_public_dict() for name in MODEL_ROLES}


def models_config_from_dict(raw: Any) -> ModelsConfig:
    """Parse the ``models`` block of a workspace ``config.json``.

    Absent (``None``) / non-mapping ⇒ the fully-defaulted (all-empty)
    :class:`ModelsConfig`, so today's resolution is unchanged. A present block
    forwards each recognised role; unknown roles are ignored.
    """
    if not isinstance(raw, Mapping):
        return ModelsConfig()
    return ModelsConfig(
        harness=role_spec_from_dict(raw.get("harness")),
        auxiliary=role_spec_from_dict(raw.get("auxiliary")),
        builder=role_spec_from_dict(raw.get("builder")),
        judge=role_spec_from_dict(raw.get("judge")),
        proposer_breadth=role_spec_from_dict(raw.get("proposer_breadth")),
        proposer_depth=role_spec_from_dict(raw.get("proposer_depth")),
    )


def load_models_config(workspace_config: Mapping[str, Any]) -> ModelsConfig:
    """Read the ``models`` block out of a loaded workspace config dict."""
    return models_config_from_dict(workspace_config.get("models"))


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def resolve_text_call_llm(spec: RoleSpec, *, role: str) -> CallLLM:
    """Resolve a TEXT ``(system, user, model) -> str`` call_llm for a role.

    Used for **harness / auxiliary / judge** — the roles that need a text
    call_llm (the builder needs an ADK model object instead; see
    :func:`resolve_builder_model`).

    * :attr:`RoleSpec.call_llm` set ⇒ import the dotted path (no ADK needed).
    * else :attr:`RoleSpec.model` set ⇒ build an ADK ``LiteLlm`` from
      ``model`` + ``endpoint`` + ``os.environ[api_key_env]`` and wrap it into
      a text call_llm via goldfive's ``make_default_adk_call_llm`` (the model
      spec needs the ``adk`` extra; a clear error is raised when it is
      missing).

    Raises :class:`ValueError` for an empty spec (the caller must gate on
    :attr:`RoleSpec.is_empty` first) or when the model spec cannot be built.
    """
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
    """Build an ADK ``LiteLlm`` (or bare model string) from a model spec.

    Mirrors the copilot's ``_resolve_model`` model-spec branch (the one
    source of LiteLlm construction in the codebase) so the harness / auxiliary
    / judge / builder roles all reach a provider the same way:

    * ``endpoint`` / ``api_key_env`` set ⇒ route through ADK's ``LiteLlm`` so
      a custom base URL / API-key env var is honoured;
    * neither set ⇒ hand the bare model string back (ADK resolves it to its
      native provider).

    Secret safety: the API key is read from :data:`os.environ` HERE and passed
    straight to ``LiteLlm``; it is never returned, logged, or stored on the
    spec. Raises a clear :class:`ImportError`-flavored :class:`ValueError`
    when the ``adk`` extra is missing.
    """
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
        if key:
            kwargs["api_key"] = key
    return LiteLlm(**kwargs)


def _resolve_model_spec_call_llm(spec: RoleSpec, *, role: str) -> CallLLM:
    """Build a text call_llm from a model spec via goldfive's ADK wrapper.

    Builds the ADK model with :func:`build_adk_model`, then wraps it into a
    ``(system, user, model) -> str`` callable through goldfive's
    ``make_default_adk_call_llm``. Both imports are lazy — the model-spec path
    needs the ``adk`` extra; a missing extra surfaces a clear error.
    """
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
    """A DEFERRED model-spec role resolution failed at its first call.

    Distinct from the eager :class:`ValueError`s the resolvers raise so the
    failure stays identifiable after it has crossed a judge boundary — see
    :func:`deferred_role_failures` for why that matters.
    """


#: Roles whose DEFERRED resolution failed in this process, ``role`` ⇒ message.
#: Written by :func:`lazy_text_call_llm`'s first-call path, read by
#: :func:`zicato._tournament_worker.main`.
_DEFERRED_ROLE_FAILURES: dict[str, str] = {}


def deferred_role_failures() -> dict[str, str]:
    """Roles whose deferred resolution failed in this process (a copy).

    The reason this register exists. Deferring resolution to the first call
    moves the failure from worker STARTUP — where a non-zero exit made the
    board unit an infra abort — to somewhere inside the run, and the judge
    path SWALLOWS exceptions by hard contract (zicato's
    ``_InlineCriterionJudge`` and goldfive's ``DefaultSteerer`` both catch
    and log, because a misbehaving judge must not crash a run). A
    misconfigured judge model would therefore score as "no signal" on every
    observation point: the unit completes, drift is undercounted, and the
    scalar is *better* than the truth. That is evaluation-data corruption
    from a config typo, so it must not be possible.

    A role-resolution failure is a deterministic CONFIGURATION fault, not a
    transient call failure, so the worker turns a non-empty register into a
    non-zero exit — restoring exactly the outcome the eager path produced.
    """
    return dict(_DEFERRED_ROLE_FAILURES)


def clear_deferred_role_failures() -> None:
    """Reset the register. For tests; a worker process resolves once."""
    _DEFERRED_ROLE_FAILURES.clear()


def lazy_text_call_llm(spec: RoleSpec, *, role: str) -> CallLLM:
    """Like :func:`resolve_text_call_llm`, but ADK is imported on FIRST CALL.

    Why this exists (RUNTIME.md §5.5.8). Every board unit runs in a fresh
    subprocess worker, and resolving a *model spec* role calls
    :func:`build_adk_model`, which pulls the whole ``google.adk`` import graph
    — measured at **0.80 s / 88 MB / 1328 modules** on top of the worker's own
    0.08 s / 32 MB. The worker resolved every configured role eagerly at
    startup, so a unit whose entry has no LLM judge (or which never reaches
    the auxiliary side) paid the ADK tax for a role it never called. This
    wrapper defers that cost to the role's first actual invocation: a role
    that IS called pays exactly the same cost, just later, so the change is
    never a regression.

    What is still EAGER, deliberately: the spec *shape* is validated here, at
    resolve time, by the same rules :func:`resolve_text_call_llm` applies. A
    ``models`` block that names neither a ``call_llm`` dotted path nor a
    ``model`` string still fails fast at worker startup, where it is
    debuggable, rather than surfacing mid-run. Only the ADK import,
    the ``LiteLlm`` construction, and goldfive's wrapper move.

    What is DEFERRED, and therefore what moves: an environment problem —
    the ``adk`` extra missing, or a ``model`` string ADK cannot resolve —
    now raises :class:`RoleResolutionError` from the first call instead of
    from startup. Such a failure is ALSO recorded in the process-wide
    register :func:`deferred_role_failures`, because the first call may be a
    judge's, and every judge boundary swallows exceptions by hard contract —
    without the register a misconfigured judge would silently score as "no
    signal" instead of failing the unit. The worker turns a non-empty
    register into a non-zero exit, so the outcome matches the eager path.

    The dotted-path form is resolved EAGERLY and returned unwrapped: it
    imports a plain callable and never touched ADK, so there is nothing to
    defer and no reason to add a layer of indirection to it.

    Each call to this function returns a DISTINCT callable object, so the
    harness/auxiliary collusion guard
    (:func:`zicato.core.workspace.assert_distinct_callables`, an identity
    comparison) behaves exactly as it does for eagerly-resolved model specs.
    """
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
    """Resolve the BUILDER role to an ADK model object (not a text call_llm).

    The builder copilot's ``LlmAgent`` takes a ``model=`` (a model object /
    string), NOT a text call_llm. So this returns:

    * the imported dotted-path object when :attr:`RoleSpec.call_llm` is set
      (a custom model object / factory — the copilot's escape hatch), or
    * the ADK model built by :func:`build_adk_model` from the model spec.

    Raises :class:`ValueError` for an empty spec.
    """
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
