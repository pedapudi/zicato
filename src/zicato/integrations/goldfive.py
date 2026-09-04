"""Build and own Goldfive runtime resources for a harness adapter."""

from __future__ import annotations

import importlib.metadata
import json
import os
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from typing import Any, cast

GOLDFIVE_REVISION = "d1d44829d43fee46c11b83c507fc772466b7f14e"
GOLDFIVE_IMPLEMENTATION_VERSION = f"git:{GOLDFIVE_REVISION}"
ZICATO_GOLDFIVE_INTEGRATION_REVISION = 1
_ZICATO_DEFAULTS = {"agent": {"call_timeout_ms": 1_800_000}}


def installed_goldfive_implementation_version() -> str | None:
    """Return the installed Goldfive Git commit recorded by PEP 610.

    Zicato pins Goldfive to an exact commit because Goldfive's package
    version does not identify that commit.  A registry wheel without VCS
    provenance therefore cannot prove that it implements the frozen
    evaluation contract.
    """
    try:
        distribution = importlib.metadata.distribution("goldfive")
        direct_url = distribution.read_text("direct_url.json")
        document = json.loads(direct_url or "{}")
        revision = document.get("vcs_info", {}).get("commit_id")
    except (importlib.metadata.PackageNotFoundError, json.JSONDecodeError):
        return None
    if isinstance(revision, str) and len(revision) == 40:
        try:
            int(revision, 16)
        except ValueError:
            pass
        else:
            return f"git:{revision}"
    return None


def _thaw_json(value: Any) -> Any:
    """Copy Zicato's immutable JSON representation into ordinary JSON containers."""
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_thaw_json(item) for item in value]
    return value


def _document(config: Mapping[str, object]) -> Any:
    from goldfive import RuntimeConfigDocument  # noqa: PLC0415

    return RuntimeConfigDocument.from_mapping(_thaw_json(config), defaults=_ZICATO_DEFAULTS)


def normalize_config(config: Mapping[str, object]) -> dict[str, Any]:
    """Return the canonical document with credential references, never values."""
    return cast(dict[str, Any], _document(config).to_mapping())


def scaffold_config() -> dict[str, Any]:
    """Return the documented Goldfive defaults used by Zicato tournaments."""
    from goldfive import RuntimeConfigDocument  # noqa: PLC0415

    return RuntimeConfigDocument.scaffold(defaults=_ZICATO_DEFAULTS)


def secret_env_names(config: Mapping[str, object]) -> tuple[str, ...]:
    """Return the credential-variable names declared by a Goldfive document."""
    return cast(tuple[str, ...], _document(config).secret_env_names)


def missing_runtime_capabilities(config: Mapping[str, object]) -> tuple[str, ...]:
    """Return installed-backend defects for a Goldfive document."""
    return cast(tuple[str, ...], _document(config).missing_runtime_capabilities())


def build_runtime_config(config: Mapping[str, object] | None) -> Any:
    """Build Goldfive's runtime, resolving credential references at this boundary."""
    if config is None:
        raise ValueError(
            "the selected adapter requires a scoring.goldfive object; add "
            '"goldfive": {} to scoring.json to select and freeze Goldfive defaults'
        )
    installed_version = installed_goldfive_implementation_version()
    if installed_version != GOLDFIVE_IMPLEMENTATION_VERSION:
        raise ValueError(
            f"the installed Goldfive implementation is {installed_version!r}, but "
            f"this Zicato build requires {GOLDFIVE_IMPLEMENTATION_VERSION}; "
            "install the pinned VCS dependency so its commit identity is available"
        )

    return _document(config).build(resolve_secret=os.environ.get)


@asynccontextmanager
async def run_context(
    config: Mapping[str, object] | None,
    target_call_llm: Any,
    *,
    judge_only: bool,
) -> AsyncGenerator[tuple[Any, dict[str, Any]], None]:
    """Build Goldfive run kwargs and close the optional judge client."""
    import goldfive  # noqa: PLC0415

    runtime = build_runtime_config(config)
    judge_call_llm: Any | None = None
    judge_model = str(runtime.judge.model or "") or None
    if runtime.judge.base_url:
        built = goldfive.make_default_openai_call_llm(runtime.judge)
        if built is None:
            raise RuntimeError(
                "Goldfive could not construct the explicitly configured built-in judge endpoint"
            )
        judge_call_llm, judge_model = built
    try:
        kwargs: dict[str, Any] = {"call_llm": target_call_llm}
        if judge_only:
            kwargs["judge_only"] = True
        if judge_call_llm is not None:
            kwargs["judge_call_llm"] = judge_call_llm
        if judge_model is not None:
            kwargs["judge_model"] = judge_model
        yield runtime, kwargs
    finally:
        await goldfive.maybe_close_call_llm(judge_call_llm, label="judge_call_llm")


__all__ = [
    "GOLDFIVE_IMPLEMENTATION_VERSION",
    "GOLDFIVE_REVISION",
    "ZICATO_GOLDFIVE_INTEGRATION_REVISION",
    "build_runtime_config",
    "installed_goldfive_implementation_version",
    "missing_runtime_capabilities",
    "normalize_config",
    "run_context",
    "scaffold_config",
    "secret_env_names",
]
