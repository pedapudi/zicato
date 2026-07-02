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
  Python callable, that wins — both the ``models`` block and the config's
  dotted path are ignored.
* Otherwise, when the workspace config carries a ``models.{harness,
  auxiliary}`` role spec, it is resolved (a dotted-path callable, or a
  model spec built into a callable — see :mod:`zicato.models_config`).
* Otherwise the factory imports the dotted path the workspace config
  stores under ``runtime.harness_call_llm`` / ``runtime.auxiliary_call_llm``.
  Missing keys raise :class:`ValueError`.
* The judge callable is resolved from ``models.judge`` when present; absent,
  it is left ``None`` so judges fall back to the auxiliary callable (today's
  behavior, via :meth:`RuntimeConfig.effective_judge_call_llm`).
* The instance id, workspace root, and seed are read from the config's
  ``runtime`` sub-dict (with ``instance_id`` defaulting to ``"default"``).

A model/endpoint is runtime INFRASTRUCTURE, not part of the evaluation
contract, so a change to the ``models`` block does not roll the epoch.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from zicato.core.types import CallLLM, RuntimeConfig
from zicato.core.workspace import assert_distinct_callables
from zicato.import_path import import_dotted_path
from zicato.models_config import load_models_config, resolve_text_call_llm


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

    # The unified ``models`` block (runtime infra, NOT part of the contract)
    # is the first source for harness / auxiliary / judge — but an explicit
    # callable kwarg still wins, and an unconfigured role falls through to
    # the legacy ``runtime.*`` dotted paths so existing workspaces are
    # untouched.
    models = load_models_config(workspace_config)

    harness = harness_call_llm
    if harness is None and not models.harness.is_empty:
        harness = resolve_text_call_llm(models.harness, role="harness")
    if harness is None:
        dotted = runtime_dict.get("harness_call_llm")
        if not dotted:
            raise ValueError(
                "workspace_config['runtime']['harness_call_llm'] is required "
                "when no harness_call_llm callable is passed explicitly "
                "and no models.harness role is configured"
            )
        harness = _import_callable(str(dotted), kind="harness_call_llm")

    aux = auxiliary_call_llm
    if aux is None and not models.auxiliary.is_empty:
        aux = resolve_text_call_llm(models.auxiliary, role="auxiliary")
    if aux is None:
        dotted = runtime_dict.get("auxiliary_call_llm")
        if not dotted:
            raise ValueError(
                "workspace_config['runtime']['auxiliary_call_llm'] is required "
                "when no auxiliary_call_llm callable is passed explicitly "
                "and no models.auxiliary role is configured"
            )
        aux = _import_callable(str(dotted), kind="auxiliary_call_llm")

    # Judges use ``models.judge`` when present; absent, ``judge_call_llm``
    # stays ``None`` and judges fall back to the auxiliary callable via
    # ``RuntimeConfig.effective_judge_call_llm`` (today's behavior).
    judge: CallLLM | None = None
    if not models.judge.is_empty:
        judge = resolve_text_call_llm(models.judge, role="judge")

    seed_raw = runtime_dict.get("seed")
    seed: int | None = int(seed_raw) if seed_raw is not None else None

    # Resolve ``parallelism`` with three-tier precedence:
    #   1. An explicit ``--parallelism`` flag, pinned into the typed
    #      config tree at CLI startup (``zicato.config.pin_overrides``).
    #      A per-invocation flag outranks the per-workspace file, so it
    #      is checked FIRST — but only when explicitly pinned, so the
    #      mere typed-config default never masks the workspace value.
    #   2. The workspace config's ``runtime`` block — the same place
    #      ``instance_id`` and ``seed`` are read.
    #   3. The typed config tree
    #      (:attr:`ZicatoConfig.runtime.parallelism` — its default of 4,
    #      or whatever an embedding application pinned).
    # ``RuntimeConfig.__post_init__`` re-validates ``parallelism >= 1``.
    from zicato.config import load_config, pinned_override  # noqa: PLC0415 — avoid import cycle

    pinned_parallelism = pinned_override("runtime", "parallelism")
    parallelism_raw = runtime_dict.get("parallelism")
    if pinned_parallelism is not None:
        parallelism = int(pinned_parallelism)
    elif parallelism_raw is not None:
        parallelism = int(parallelism_raw)
    else:
        parallelism = load_config().runtime.parallelism

    # Worker env-scrub: opt-in containment read from the same ``runtime``
    # block. Absent ⇒ off (full env inheritance — today's behavior, byte-for-
    # byte unchanged). ``worker_env_passthrough`` is an optional list of extra
    # env-var names a scrubbed worker should still receive.
    scrub_worker_env = bool(runtime_dict.get("scrub_worker_env", False))
    passthrough_raw = runtime_dict.get("worker_env_passthrough") or ()
    worker_env_passthrough = tuple(str(name) for name in passthrough_raw)

    # Field-diversity overlap ceiling for the multi-challenger path: an
    # opt-in runtime knob read from the same ``runtime`` block. Absent /
    # null ⇒ ``None`` (enforcement off — today's behavior, byte-for-byte
    # unchanged). ``RuntimeConfig.__post_init__`` re-validates the (0, 1]
    # bound.
    tolerance_raw = runtime_dict.get("diversity_tolerance")
    diversity_tolerance = float(tolerance_raw) if tolerance_raw is not None else None

    # Endpoint-outage circuit (WS-H): opt-in runtime knobs read from the same
    # ``runtime`` block. Absent ⇒ the dataclass defaults (threshold 0 = the
    # circuit is OFF — today's behavior, byte-for-byte unchanged).
    # ``RuntimeConfig.__post_init__`` re-validates the >= 0 bounds.
    from zicato.core.runtime import (  # noqa: PLC0415
        INFRA_BACKOFF_BASE_S_DEFAULT,
        INFRA_BACKOFF_CAP_S_DEFAULT,
    )

    infra_threshold_raw = runtime_dict.get("infra_abort_round_threshold")
    infra_abort_round_threshold = int(infra_threshold_raw) if infra_threshold_raw is not None else 0
    infra_base_raw = runtime_dict.get("infra_backoff_base_s")
    infra_backoff_base_s = (
        float(infra_base_raw) if infra_base_raw is not None else INFRA_BACKOFF_BASE_S_DEFAULT
    )
    infra_cap_raw = runtime_dict.get("infra_backoff_cap_s")
    infra_backoff_cap_s = (
        float(infra_cap_raw) if infra_cap_raw is not None else INFRA_BACKOFF_CAP_S_DEFAULT
    )

    # Inner ADK agent model: when ``models.harness`` is a *model spec* (a
    # model string, optionally + endpoint/api_key_env), build the ADK model
    # object so the adapter can rebind the target's agents to it with native
    # tool/function calling intact (the config-driven alternative to a bare
    # string + the text-only shim). A dotted ``call_llm`` harness role, or an
    # endpoint-less spec that yields a bare string, leaves ``inner_model`` None
    # — the adapter then uses its guarded shim rebind, exactly as before.
    inner_model: Any = None
    if not models.harness.is_empty and models.harness.model:
        from zicato.models_config import build_adk_model  # noqa: PLC0415

        try:
            built = build_adk_model(models.harness, role="harness")
        except ValueError:
            built = None  # ADK/litellm unavailable — fall back to the shim path.
        if built is not None and not isinstance(built, str):
            inner_model = built

    # Defense in depth — also re-checked by the runner.
    assert_distinct_callables(harness, aux)

    return RuntimeConfig(
        instance_id=instance_id,
        workspace_root=resolved_root,
        harness_call_llm=harness,
        auxiliary_call_llm=aux,
        seed=seed,
        parallelism=parallelism,
        judge_call_llm=judge,
        scrub_worker_env=scrub_worker_env,
        worker_env_passthrough=worker_env_passthrough,
        diversity_tolerance=diversity_tolerance,
        inner_model=inner_model,
        infra_abort_round_threshold=infra_abort_round_threshold,
        infra_backoff_base_s=infra_backoff_base_s,
        infra_backoff_cap_s=infra_backoff_cap_s,
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
