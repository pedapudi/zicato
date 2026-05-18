"""Build a :class:`RuntimeConfig` from a workspace config dict.

Centralises the lookup-and-validate step that every entry point (CLI
tournament command, the orchestrator's ``evolve`` loop, tests with
real callables) wants to perform exactly once before handing the
config to the runner. Importantly, it routes through
:func:`zicato.core.workspace.assert_distinct_callables` so the
two-callable invariant on :class:`RuntimeConfig` is enforced at
construction.

Resolution rules:

* If the caller supplies a ``harness_call_llm`` / ``auxiliary_call_llm``
  Python callable, that wins — the config's dotted path is ignored.
* Otherwise the factory imports the dotted path the workspace config
  stores under ``runtime.harness_call_llm`` / ``runtime.auxiliary_call_llm``.
  Missing keys raise :class:`ValueError`.
* The instance id, workspace root, and seed are read from the config's
  ``runtime`` sub-dict (with ``instance_id`` defaulting to ``"default"``).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from zicato.core.types import CallLLM, RuntimeConfig
from zicato.core.workspace import assert_distinct_callables
from zicato.import_path import import_dotted_path


def make_runtime_config(
    workspace_config: Mapping[str, Any],
    *,
    workspace_root: Path | None = None,
    harness_call_llm: CallLLM | None = None,
    auxiliary_call_llm: CallLLM | None = None,
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
        * ``runtime.harness_call_llm`` (dotted path; only consulted
          when the kwarg is ``None``).
        * ``runtime.auxiliary_call_llm`` (dotted path; same rule).
        * ``runtime.seed`` (int or null).
    workspace_root:
        Optional override for the workspace root path. When ``None``
        we fall back to ``config['runtime']['workspace_root']`` and then
        to ``.zicato`` (relative to the operator's cwd).
    harness_call_llm, auxiliary_call_llm:
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
    runtime_dict = workspace_config.get("runtime", {}) or {}
    if not isinstance(runtime_dict, Mapping):
        raise ValueError(
            f"workspace_config['runtime'] must be a mapping, got " f"{type(runtime_dict).__name__}"
        )

    instance_id = str(runtime_dict.get("instance_id", "default"))

    resolved_root: Path
    if workspace_root is not None:
        resolved_root = Path(workspace_root)
    else:
        raw_root = runtime_dict.get("workspace_root", ".zicato")
        resolved_root = Path(str(raw_root))

    harness = harness_call_llm
    if harness is None:
        dotted = runtime_dict.get("harness_call_llm")
        if not dotted:
            raise ValueError(
                "workspace_config['runtime']['harness_call_llm'] is required "
                "when no harness_call_llm callable is passed explicitly"
            )
        harness = _import_callable(str(dotted), kind="harness_call_llm")

    aux = auxiliary_call_llm
    if aux is None:
        dotted = runtime_dict.get("auxiliary_call_llm")
        if not dotted:
            raise ValueError(
                "workspace_config['runtime']['auxiliary_call_llm'] is required "
                "when no auxiliary_call_llm callable is passed explicitly"
            )
        aux = _import_callable(str(dotted), kind="auxiliary_call_llm")

    seed_raw = runtime_dict.get("seed")
    seed: int | None = int(seed_raw) if seed_raw is not None else None

    # Resolve ``parallelism`` with three-tier precedence:
    #   1. The workspace config's ``runtime`` block — the same place
    #      ``instance_id`` and ``seed`` are read, so an explicit per-
    #      workspace value wins.
    #   2. The typed config tree's env-backed field
    #      (:attr:`ZicatoConfig.runtime.parallelism`, bound to
    #      ``ZICATO_PARALLELISM``).
    #   3. The :class:`RuntimeConfig` default of 4.
    # ``RuntimeConfig.__post_init__`` re-validates ``parallelism >= 1``.
    parallelism_raw = runtime_dict.get("parallelism")
    if parallelism_raw is not None:
        parallelism = int(parallelism_raw)
    else:
        from zicato.config import load_config  # noqa: PLC0415 — avoid import cycle

        parallelism = load_config().runtime.parallelism

    # Defense in depth — also re-checked by the runner.
    assert_distinct_callables(harness, aux)

    return RuntimeConfig(
        instance_id=instance_id,
        workspace_root=resolved_root,
        harness_call_llm=harness,
        auxiliary_call_llm=aux,
        seed=seed,
        parallelism=parallelism,
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


__all__ = ["make_runtime_config"]
