"""``builder.json`` — the tournament-builder backend's own config.

The builder is the deterministic backend the tournament-builder form
(B2) and the copilot (B1b) both drive. This module owns its *own*
configuration file — distinct from the workspace ``config.json`` and the
per-epoch ``scoring.json`` — which records how the copilot reaches a
model, which builder skills it composes, and an optional UI theme.

The file is read-only here: B1a never writes ``builder.json``. It is
located at ``<workspace>/builder.json`` or ``<workspace>/.zicato/builder.json``;
absent ⇒ every field defaults, the model is empty, and chat is disabled.

Secret safety
-------------
The config records only the *name* of an environment variable that holds
the API key (:attr:`BuilderAgentConfig.api_key_env`) — never the key's
value. :meth:`BuilderConfig.to_public_dict` is the only surface the REST
layer serializes; it carries the env-var name through but never resolves
it, so a secret can never leak to the UI.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: The default builder skills the copilot composes when ``builder.json``
#: does not override them. These are the design/workflow skills the
#: tournament-builder copilot loads to walk an operator through a build.
DEFAULT_SKILLS: tuple[str, ...] = (
    "zicato-build-tournament",
    "zicato-build-board",
)


@dataclass(frozen=True, slots=True)
class BuilderAgentConfig:
    """How the copilot (B1b) reaches a model.

    Loaded here, consumed by B1b — B1a never calls a model. Every field
    is optional so an absent / partial ``builder.json`` yields a config
    whose empty :attr:`model` disables chat.

    Fields
    ------
    model:
        The model identifier the copilot passes through to its
        ``call_llm`` callable. Empty string ⇒ no model configured ⇒ chat
        disabled (surfaced via :attr:`BuilderConfig.chat_enabled`).
    endpoint:
        Optional base URL / endpoint the copilot's provider should hit.
        ``None`` ⇒ the provider's default.
    api_key_env:
        The *name* of the environment variable holding the provider API
        key — never the key itself. ``None`` ⇒ no credential indirection.
    call_llm:
        Optional dotted path to a ``call_llm`` callable factory the
        copilot resolves at runtime. ``None`` ⇒ the builder's default.
    """

    model: str = ""
    endpoint: str | None = None
    api_key_env: str | None = None
    call_llm: str | None = None

    def to_public_dict(self) -> dict[str, Any]:
        """Serialize for the UI — carries the env-var *name*, never a secret.

        Only :attr:`api_key_env` (a variable name) is emitted; the
        variable's value is never read here, so no credential can leak.
        """
        return {
            "model": self.model,
            "endpoint": self.endpoint,
            "api_key_env": self.api_key_env,
            "call_llm": self.call_llm,
        }


@dataclass(frozen=True, slots=True)
class BuilderConfig:
    """The builder backend's resolved configuration.

    Fields
    ------
    agent:
        How the copilot reaches a model (see :class:`BuilderAgentConfig`).
    skills:
        The builder skills the copilot composes. Defaults to
        :data:`DEFAULT_SKILLS`.
    theme:
        Optional UI theme name, or ``None`` for the default.
    """

    agent: BuilderAgentConfig = field(default_factory=BuilderAgentConfig)
    skills: tuple[str, ...] = DEFAULT_SKILLS
    theme: str | None = None

    @property
    def chat_enabled(self) -> bool:
        """``True`` iff a non-empty model is configured.

        An empty model means the copilot cannot reach a provider, so the
        UI disables the chat panel and falls back to form-only editing.
        """
        return bool(self.agent.model)

    def to_public_dict(self) -> dict[str, Any]:
        """Serialize for the UI — NEVER includes a secret value.

        The nested agent config carries only the env-var *name* for the
        API key. ``chat_enabled`` is folded in so the UI does not have to
        re-derive it.
        """
        return {
            "agent": self.agent.to_public_dict(),
            "skills": list(self.skills),
            "theme": self.theme,
            "chat_enabled": self.chat_enabled,
        }


def _builder_config_path(workspace_root: Path) -> Path | None:
    """Resolve the ``builder.json`` path for a workspace, or ``None``.

    Accepts the workspace root either as the project root (the file lives
    at ``<root>/builder.json``) or as a ``.zicato`` directory (the file
    lives at ``<root>/builder.json`` directly). Both
    ``<workspace>/builder.json`` and ``<workspace>/.zicato/builder.json``
    are probed so callers may pass whichever they have.
    """
    root = Path(workspace_root)
    candidates = [root / "builder.json", root / ".zicato" / "builder.json"]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _agent_from_dict(raw: Any) -> BuilderAgentConfig:
    """Parse the ``agent`` block of ``builder.json`` into a typed config.

    Absent / non-mapping ⇒ the fully-defaulted (empty-model) agent
    config. Unknown keys are ignored so a forward-compatible file loads.
    """
    if not isinstance(raw, Mapping):
        return BuilderAgentConfig()
    return BuilderAgentConfig(
        model=str(raw.get("model", "") or ""),
        endpoint=_opt_str(raw.get("endpoint")),
        api_key_env=_opt_str(raw.get("api_key_env")),
        call_llm=_opt_str(raw.get("call_llm")),
    )


def _opt_str(value: Any) -> str | None:
    """Coerce an optional JSON value into ``str | None``.

    Empty strings collapse to ``None`` so a blank field reads the same as
    an absent one.
    """
    if value is None:
        return None
    text = str(value)
    return text or None


def load_builder_config(workspace_root: Path) -> BuilderConfig:
    """Load ``builder.json`` for a workspace, or return defaults.

    Probes ``<workspace>/builder.json`` then
    ``<workspace>/.zicato/builder.json`` (see :func:`_builder_config_path`).
    An absent file ⇒ a fully-defaulted :class:`BuilderConfig` (empty
    model, default skills, no theme), so a workspace that never configures
    the builder still loads — with chat disabled.

    A malformed (non-object) JSON top level raises :class:`ValueError`.
    """
    path = _builder_config_path(workspace_root)
    if path is None:
        return BuilderConfig()
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"could not parse {path}: {exc.msg}") from exc
    if not isinstance(loaded, Mapping):
        raise ValueError(
            f"{path}: expected a JSON object at top level, got {type(loaded).__name__}"
        )

    raw_skills = loaded.get("skills")
    if isinstance(raw_skills, list | tuple) and raw_skills:
        skills = tuple(str(s) for s in raw_skills)
    else:
        skills = DEFAULT_SKILLS

    return BuilderConfig(
        agent=_agent_from_dict(loaded.get("agent")),
        skills=skills,
        theme=_opt_str(loaded.get("theme")),
    )


__all__ = [
    "DEFAULT_SKILLS",
    "BuilderAgentConfig",
    "BuilderConfig",
    "load_builder_config",
]
