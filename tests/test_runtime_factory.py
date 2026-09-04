"""Tests for :mod:`zicato.runtime_factory`."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from zicato.runtime_factory import make_runtime_config


async def _stub_target(system: str, user: str, model: str) -> str:
    del system, user, model
    return ""


async def _stub_aux(system: str, user: str, model: str) -> str:
    del system, user, model
    return ""


def test_runtime_config_from_explicit_callables(tmp_path: Path) -> None:
    cfg = make_runtime_config(
        {"runtime": {"instance_id": "smoke"}},
        workspace_root=tmp_path,
        target_call_llm=_stub_target,
        evaluation_call_llm=_stub_aux,
    )
    assert cfg.instance_id == "smoke"
    assert cfg.workspace_root == tmp_path
    assert cfg.target_call_llm is _stub_target
    assert cfg.evaluation_call_llm is _stub_aux


def test_runtime_config_seed_passes_through(tmp_path: Path) -> None:
    cfg = make_runtime_config(
        {"runtime": {"seed": 1234}},
        workspace_root=tmp_path,
        target_call_llm=_stub_target,
        evaluation_call_llm=_stub_aux,
    )
    assert cfg.seed == 1234


def test_runtime_config_scrub_worker_env_defaults_off(tmp_path: Path) -> None:
    """Absent ``runtime.scrub_worker_env`` ⇒ off (today's full-inheritance)."""
    cfg = make_runtime_config(
        {"runtime": {}},
        workspace_root=tmp_path,
        target_call_llm=_stub_target,
        evaluation_call_llm=_stub_aux,
    )
    assert cfg.scrub_worker_env is False
    assert cfg.worker_env_passthrough == ()


def test_runtime_config_scrub_worker_env_opt_in(tmp_path: Path) -> None:
    """``runtime.scrub_worker_env`` + passthrough list are read into the config."""
    cfg = make_runtime_config(
        {
            "runtime": {
                "scrub_worker_env": True,
                "worker_env_passthrough": ["CUSTOM_A", "CUSTOM_B"],
            }
        },
        workspace_root=tmp_path,
        target_call_llm=_stub_target,
        evaluation_call_llm=_stub_aux,
    )
    assert cfg.scrub_worker_env is True
    assert cfg.worker_env_passthrough == ("CUSTOM_A", "CUSTOM_B")


def test_runtime_config_diversity_tolerance_defaults_off(tmp_path: Path) -> None:
    """Absent ``runtime.diversity_tolerance`` ⇒ ``None`` (enforcement off)."""
    cfg = make_runtime_config(
        {"runtime": {}},
        workspace_root=tmp_path,
        target_call_llm=_stub_target,
        evaluation_call_llm=_stub_aux,
    )
    assert cfg.diversity_tolerance is None


def test_runtime_config_diversity_tolerance_opt_in(tmp_path: Path) -> None:
    """``runtime.diversity_tolerance`` is read into the config as a float."""
    cfg = make_runtime_config(
        {"runtime": {"diversity_tolerance": 0.5}},
        workspace_root=tmp_path,
        target_call_llm=_stub_target,
        evaluation_call_llm=_stub_aux,
    )
    assert cfg.diversity_tolerance == 0.5


def test_runtime_config_diversity_tolerance_out_of_range_raises(tmp_path: Path) -> None:
    """An out-of-(0, 1] tolerance is rejected at construction."""
    with pytest.raises(ValueError, match="diversity_tolerance"):
        make_runtime_config(
            {"runtime": {"diversity_tolerance": 1.5}},
            workspace_root=tmp_path,
            target_call_llm=_stub_target,
            evaluation_call_llm=_stub_aux,
        )


def test_runtime_config_parallelism_workspace_value_wins_over_default(
    tmp_path: Path,
) -> None:
    """An explicit ``runtime.parallelism`` in the workspace config wins
    over the typed-config default (no flag pinned)."""
    cfg = make_runtime_config(
        {"runtime": {"parallelism": 3}},
        workspace_root=tmp_path,
        target_call_llm=_stub_target,
        evaluation_call_llm=_stub_aux,
    )
    assert cfg.parallelism == 3


def test_runtime_config_parallelism_pinned_flag_wins_over_workspace(
    tmp_path: Path,
) -> None:
    """A pinned ``--parallelism`` flag outranks the workspace config.

    The flag is a per-invocation operator decision; the workspace
    ``config.json`` is the per-workspace default. The pin is applied
    exactly as the evolve CLI applies it.
    """
    from zicato.config import pin_overrides

    pin_overrides({"runtime": {"parallelism": 7}})
    cfg = make_runtime_config(
        {"runtime": {"parallelism": 3}},
        workspace_root=tmp_path,
        target_call_llm=_stub_target,
        evaluation_call_llm=_stub_aux,
    )
    assert cfg.parallelism == 7


def test_runtime_config_parallelism_env_var_is_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The deleted ``ZICATO_PARALLELISM`` env var is ignored entirely."""
    monkeypatch.setenv("ZICATO_PARALLELISM", "9")
    cfg = make_runtime_config(
        {"runtime": {}},
        workspace_root=tmp_path,
        target_call_llm=_stub_target,
        evaluation_call_llm=_stub_aux,
    )
    assert cfg.parallelism == 4


def test_runtime_config_parallelism_default(tmp_path: Path) -> None:
    """With neither a workspace value nor a pinned flag, the default 4 holds."""
    cfg = make_runtime_config(
        {},
        workspace_root=tmp_path,
        target_call_llm=_stub_target,
        evaluation_call_llm=_stub_aux,
    )
    assert cfg.parallelism == 4


def test_host_worker_permits_defaults_to_auto(tmp_path: Path) -> None:
    """Absent from the ``runtime`` block ⇒ ``None`` = AUTO (max(4, 2 x cores)).

    ``parallelism`` bounds THIS process; ``host_worker_permits`` bounds the
    host across orchestrators (RUNTIME.md §5.5.7).
    """
    cfg = make_runtime_config(
        {},
        workspace_root=tmp_path,
        target_call_llm=_stub_target,
        evaluation_call_llm=_stub_aux,
    )
    assert cfg.host_worker_permits is None


def test_host_worker_permits_reads_the_runtime_block(tmp_path: Path) -> None:
    """An explicit ceiling — and ``0``, the explicit off switch — pass through."""
    cfg = make_runtime_config(
        {"runtime": {"host_worker_permits": 6}},
        workspace_root=tmp_path,
        target_call_llm=_stub_target,
        evaluation_call_llm=_stub_aux,
    )
    assert cfg.host_worker_permits == 6

    off = make_runtime_config(
        {"runtime": {"host_worker_permits": 0}},
        workspace_root=tmp_path,
        target_call_llm=_stub_target,
        evaluation_call_llm=_stub_aux,
    )
    assert off.host_worker_permits == 0


def test_worker_permit_directory_and_log_level_read_the_runtime_block(tmp_path: Path) -> None:
    permit_dir = tmp_path / "shared-worker-permits"
    cfg = make_runtime_config(
        {
            "runtime": {
                "worker_permit_dir": str(permit_dir),
                "log_level": "debug",
            }
        },
        workspace_root=tmp_path,
        target_call_llm=_stub_target,
        evaluation_call_llm=_stub_aux,
    )
    assert cfg.worker_permit_dir == permit_dir
    assert cfg.log_level == "DEBUG"


@pytest.mark.parametrize("value", ["./shared-worker-permits", "", 42])
def test_worker_permit_directory_requires_an_absolute_path(
    tmp_path: Path,
    value: object,
) -> None:
    with pytest.raises(ValueError, match="worker_permit_dir must be .*absolute path"):
        make_runtime_config(
            {"runtime": {"worker_permit_dir": value}},
            workspace_root=tmp_path,
            target_call_llm=_stub_target,
            evaluation_call_llm=_stub_aux,
        )


def test_runtime_log_level_rejects_an_unknown_name(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="RuntimeConfig.log_level"):
        make_runtime_config(
            {"runtime": {"log_level": "verbose"}},
            workspace_root=tmp_path,
            target_call_llm=_stub_target,
            evaluation_call_llm=_stub_aux,
        )


def test_host_worker_permits_reads_a_json_boolean_as_intent(tmp_path: Path) -> None:
    """``true`` means AUTO, not ``int(True) == 1``.

    The knob name reads boolean-ish, so "on" is a plausible thing to write —
    and one permit host-wide would silently serialise every concurrent run
    down to a single worker, which is emphatically not what "on" meant.
    """
    on = make_runtime_config(
        {"runtime": {"host_worker_permits": True}},
        workspace_root=tmp_path,
        target_call_llm=_stub_target,
        evaluation_call_llm=_stub_aux,
    )
    assert on.host_worker_permits is None, "true must mean AUTO, never a cap of 1"

    off = make_runtime_config(
        {"runtime": {"host_worker_permits": False}},
        workspace_root=tmp_path,
        target_call_llm=_stub_target,
        evaluation_call_llm=_stub_aux,
    )
    assert off.host_worker_permits == 0


def test_runtime_config_default_instance_id(tmp_path: Path) -> None:
    cfg = make_runtime_config(
        {},
        workspace_root=tmp_path,
        target_call_llm=_stub_target,
        evaluation_call_llm=_stub_aux,
    )
    assert cfg.instance_id == "default"


def test_runtime_config_rejects_shared_callable(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="must be distinct"):
        make_runtime_config(
            {},
            workspace_root=tmp_path,
            target_call_llm=_stub_target,
            evaluation_call_llm=_stub_target,
        )


def test_runtime_config_resolves_dotted_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_mod = types.ModuleType("fake_runtime_mod")
    fake_mod.target_fn = _stub_target  # type: ignore[attr-defined]
    fake_mod.aux_fn = _stub_aux  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fake_runtime_mod", fake_mod)

    cfg = make_runtime_config(
        {
            "runtime": {
                "target_call_llm": "fake_runtime_mod.target_fn",
                "evaluation_call_llm": "fake_runtime_mod:aux_fn",
            }
        },
        workspace_root=tmp_path,
    )
    assert cfg.target_call_llm is _stub_target
    assert cfg.evaluation_call_llm is _stub_aux


def test_runtime_config_missing_harness_dotted_path_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="target_call_llm"):
        make_runtime_config({}, workspace_root=tmp_path)


def test_runtime_config_missing_aux_dotted_path_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="evaluation_call_llm"):
        make_runtime_config(
            {},
            workspace_root=tmp_path,
            target_call_llm=_stub_target,
        )


def test_runtime_config_bad_dotted_path_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="could not import"):
        make_runtime_config(
            {
                "runtime": {
                    "target_call_llm": "nope_module.attr",
                    "evaluation_call_llm": "nope_module.attr_other",
                }
            },
            workspace_root=tmp_path,
        )


def test_runtime_config_non_callable_dotted_path_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_mod = types.ModuleType("noncallable_mod")
    fake_mod.value = 42  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "noncallable_mod", fake_mod)

    with pytest.raises(ValueError, match="expected a callable"):
        make_runtime_config(
            {
                "runtime": {
                    "target_call_llm": "noncallable_mod.value",
                    "evaluation_call_llm": "noncallable_mod.value",
                }
            },
            workspace_root=tmp_path,
        )


# ---------------------------------------------------------------------------
# Unified ``models`` block resolution (runtime infra, not the contract)
# ---------------------------------------------------------------------------


def _install_models_callables(monkeypatch: pytest.MonkeyPatch) -> None:
    """Register three distinct module-level call_llm callables for dotted paths."""
    mod = types.ModuleType("fake_models_mod")
    mod.target_fn = _stub_target  # type: ignore[attr-defined]
    mod.aux_fn = _stub_aux  # type: ignore[attr-defined]

    async def _judge(system: str, user: str, model: str) -> str:
        del system, user, model
        return ""

    mod.judge_fn = _judge  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fake_models_mod", mod)


def _models(**specs: dict[str, object]) -> dict[str, object]:
    public = {
        "proposer_breadth": "proposer_generate",
        "proposer_depth": "proposer_review",
    }
    engines = {public.get(role, role): spec for role, spec in specs.items()}
    roles = {
        public.get(role, role): public.get(role, role)
        for role in specs
        if role not in {"target", "evaluation"}
    }
    return {"engines": engines, "roles": roles}


def test_models_block_resolves_harness_and_auxiliary_dotted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``models.{harness,evaluation}`` (dotted form) wins over ``runtime.*``."""
    _install_models_callables(monkeypatch)
    cfg = make_runtime_config(
        {
            # legacy runtime.* present but should be ignored when models.* set.
            "runtime": {
                "target_call_llm": "fake_models_mod.judge_fn",
                "evaluation_call_llm": "fake_models_mod.judge_fn",
            },
            "models": _models(
                target={"call_llm": "fake_models_mod:target_fn"},
                evaluation={"call_llm": "fake_models_mod:aux_fn"},
            ),
        },
        workspace_root=tmp_path,
    )
    assert cfg.target_call_llm is _stub_target
    assert cfg.evaluation_call_llm is _stub_aux


def test_models_block_falls_back_to_runtime_when_role_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unconfigured ``models`` role falls back to ``runtime.*`` (today's path)."""
    _install_models_callables(monkeypatch)
    cfg = make_runtime_config(
        {
            "runtime": {
                "target_call_llm": "fake_models_mod.target_fn",
                "evaluation_call_llm": "fake_models_mod.aux_fn",
            },
            # empty models block ⇒ both roles fall through to runtime.*
            "models": {"engines": {}, "roles": {}},
        },
        workspace_root=tmp_path,
    )
    assert cfg.target_call_llm is _stub_target
    assert cfg.evaluation_call_llm is _stub_aux
    # No judge role ⇒ judge_call_llm is None and judges use the evaluation callable.
    assert cfg.judge_call_llm is None
    assert cfg.effective_judge_call_llm() is _stub_aux


def test_explicit_callable_kwarg_beats_models_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An explicit callable kwarg wins over a configured ``models`` role."""
    _install_models_callables(monkeypatch)
    cfg = make_runtime_config(
        {"models": _models(target={"call_llm": "fake_models_mod:target_fn"})},
        workspace_root=tmp_path,
        target_call_llm=_stub_aux,  # the kwarg wins
        evaluation_call_llm=_stub_target,
    )
    assert cfg.target_call_llm is _stub_aux


def test_models_target_endpoint_spec_builds_target_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A ``models.target`` *model spec* with an endpoint builds the inner ADK
    model so the adapter can rebind the target's agents to it (function-calling
    against the configured endpoint), not the text-only shim."""
    pytest.importorskip("litellm")
    from google.adk.models.lite_llm import LiteLlm

    _install_models_callables(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-dummy")
    cfg = make_runtime_config(
        {
            "models": _models(
                target={
                    "model": "openai/gemma-4-26B-A4B-it-FP8",
                    "endpoint": "http://kossel.lan:8080/v1",
                    "api_key_env": "OPENAI_API_KEY",
                },
                evaluation={"call_llm": "fake_models_mod:aux_fn"},
            )
        },
        workspace_root=tmp_path,
    )
    assert isinstance(cfg.target_model, LiteLlm)


def test_dotted_target_role_leaves_target_model_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A dotted ``call_llm`` target role configures no inner model — the
    adapter falls back to its guarded shim rebind (today's behaviour)."""
    _install_models_callables(monkeypatch)
    cfg = make_runtime_config(
        {
            "models": _models(
                target={"call_llm": "fake_models_mod:target_fn"},
                evaluation={"call_llm": "fake_models_mod:aux_fn"},
            )
        },
        workspace_root=tmp_path,
    )
    assert cfg.target_model is None


def test_models_judge_role_resolves_and_overrides_evaluation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``models.judge`` resolves to a distinct judge callable; else aux is used."""
    _install_models_callables(monkeypatch)
    cfg = make_runtime_config(
        {
            "models": _models(
                target={"call_llm": "fake_models_mod:target_fn"},
                evaluation={"call_llm": "fake_models_mod:aux_fn"},
                judge={"call_llm": "fake_models_mod:judge_fn"},
            )
        },
        workspace_root=tmp_path,
    )
    assert cfg.judge_call_llm is not None
    # The judge callable is the configured one, NOT the evaluation callable.
    assert cfg.effective_judge_call_llm() is cfg.judge_call_llm
    assert cfg.effective_judge_call_llm() is not cfg.evaluation_call_llm


def test_models_collusion_guard_fires_when_harness_equals_evaluation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The collusion guard still rejects target==evaluation via the models block."""
    _install_models_callables(monkeypatch)
    with pytest.raises(RuntimeError, match="must be distinct"):
        make_runtime_config(
            {
                "models": _models(
                    target={"call_llm": "fake_models_mod:target_fn"},
                    evaluation={"call_llm": "fake_models_mod:target_fn"},
                )
            },
            workspace_root=tmp_path,
        )


# ---------------------------------------------------------------------------
# WS-ENS ensemble proposer roles (breadth + depth)
# ---------------------------------------------------------------------------


def _install_proposer_role_callables(monkeypatch: pytest.MonkeyPatch) -> None:
    """Register distinct breadth / depth call_llm callables for dotted paths."""

    async def _breadth(system: str, user: str, model: str) -> str:
        del system, user, model
        return "breadth"

    async def _depth(system: str, user: str, model: str) -> str:
        del system, user, model
        return "depth"

    mod = types.ModuleType("fake_proposer_roles_mod")
    mod.breadth_fn = _breadth  # type: ignore[attr-defined]
    mod.depth_fn = _depth  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fake_proposer_roles_mod", mod)


def test_proposer_roles_default_none_and_fall_back_to_evaluation(tmp_path: Path) -> None:
    """Absent ``models.proposer_{breadth,depth}`` ⇒ ``None`` and the effective
    accessors resolve to the SAME evaluation callable object (byte-identical)."""
    cfg = make_runtime_config(
        {},
        workspace_root=tmp_path,
        target_call_llm=_stub_target,
        evaluation_call_llm=_stub_aux,
    )
    assert cfg.proposer_breadth_call_llm is None
    assert cfg.proposer_depth_call_llm is None
    assert cfg.proposer_breadth_model is None
    assert cfg.proposer_depth_model is None
    # The fall-back is the evaluation callable — the very object sampling +
    # critique always used, so an unconfigured ensemble is byte-identical.
    assert cfg.effective_proposer_breadth_call_llm() is _stub_aux
    assert cfg.effective_proposer_depth_call_llm() is _stub_aux


def test_proposer_roles_resolve_from_models_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``models.proposer_breadth`` / ``proposer_depth`` (dotted form) resolve to
    their own callables, distinct from the evaluation surface."""
    _install_models_callables(monkeypatch)
    _install_proposer_role_callables(monkeypatch)
    cfg = make_runtime_config(
        {
            "models": _models(
                target={"call_llm": "fake_models_mod:target_fn"},
                evaluation={"call_llm": "fake_models_mod:aux_fn"},
                proposer_breadth={"call_llm": "fake_proposer_roles_mod:breadth_fn"},
                proposer_depth={"call_llm": "fake_proposer_roles_mod:depth_fn"},
            )
        },
        workspace_root=tmp_path,
    )
    assert cfg.proposer_breadth_call_llm is not None
    assert cfg.proposer_depth_call_llm is not None
    assert cfg.effective_proposer_breadth_call_llm() is cfg.proposer_breadth_call_llm
    assert cfg.effective_proposer_depth_call_llm() is cfg.proposer_depth_call_llm
    assert cfg.effective_proposer_breadth_call_llm() is not cfg.evaluation_call_llm
    assert cfg.effective_proposer_depth_call_llm() is not cfg.evaluation_call_llm
    # A call_llm (dotted) role has NO model name — the model-string thread stays
    # None, so it steers only proposers that read ``ctx.aux_call_llm``.
    assert cfg.proposer_breadth_model is None
    assert cfg.proposer_depth_model is None


def test_base_proposer_role_routes_text_callable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_models_callables(monkeypatch)
    _install_proposer_role_callables(monkeypatch)
    cfg = make_runtime_config(
        {
            "models": _models(
                target={"call_llm": "fake_models_mod:target_fn"},
                evaluation={"call_llm": "fake_models_mod:aux_fn"},
                proposer={"call_llm": "fake_proposer_roles_mod:breadth_fn"},
            )
        },
        workspace_root=tmp_path,
    )
    assert cfg.effective_proposer_call_llm() is cfg.proposer_call_llm
    assert cfg.proposer_call_llm is not cfg.evaluation_call_llm
    assert cfg.proposer_model is None


def test_base_proposer_model_is_available_to_native_and_process_agents(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_models_callables(monkeypatch)

    def resolve(spec: object, *, role: str):  # type: ignore[no-untyped-def]
        del spec
        return _stub_target if role == "target" else _stub_aux

    monkeypatch.setattr("zicato.runtime_factory.resolve_text_call_llm", resolve)
    cfg = make_runtime_config(
        {
            "models": {
                "engines": {
                    "target": {"call_llm": "fake_models_mod:target_fn"},
                    "evaluation": {"call_llm": "fake_models_mod:aux_fn"},
                    "strong": {"model": "house-strong"},
                },
                "roles": {"proposer": "strong"},
            }
        },
        workspace_root=tmp_path,
    )
    assert cfg.proposer_model == "house-strong"
    assert cfg.proposer_breadth_model == "house-strong"
    assert cfg.proposer_depth_model == "house-strong"


def test_proposer_roles_model_spec_captures_model_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A ``models.proposer_{breadth,depth}`` *model spec* captures its model-name
    string onto the config so the wrapper can thread it onto ``ctx.model`` (the
    default ADK proposer's binding)."""
    pytest.importorskip("litellm")
    _install_models_callables(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-dummy")
    cfg = make_runtime_config(
        {
            "models": _models(
                target={"call_llm": "fake_models_mod:target_fn"},
                evaluation={"call_llm": "fake_models_mod:aux_fn"},
                proposer_breadth={
                    "model": "openai/breadth-model",
                    "endpoint": "http://kossel.lan:8080/v1",
                    "api_key_env": "OPENAI_API_KEY",
                },
                proposer_depth={
                    "model": "openai/depth-model",
                    "endpoint": "http://kossel.lan:8080/v1",
                    "api_key_env": "OPENAI_API_KEY",
                },
            )
        },
        workspace_root=tmp_path,
    )
    assert cfg.proposer_breadth_model == "openai/breadth-model"
    assert cfg.proposer_depth_model == "openai/depth-model"
    # The callables resolved too (both threads carry the spec).
    assert cfg.proposer_breadth_call_llm is not None
    assert cfg.proposer_depth_call_llm is not None


def test_proposer_roles_no_collusion_guard_between_breadth_and_depth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Breadth and depth may be the SAME callable — one proposer-side trust
    domain, so the collusion identity-guard does NOT apply between them.

    (Contrast ``test_models_collusion_guard_fires_when_harness_equals_evaluation``:
    that guard is for evaluator-vs-evaluated separation, not proposer roles.)
    """
    _install_models_callables(monkeypatch)
    _install_proposer_role_callables(monkeypatch)
    cfg = make_runtime_config(
        {
            "models": _models(
                target={"call_llm": "fake_models_mod:target_fn"},
                evaluation={"call_llm": "fake_models_mod:aux_fn"},
                # deliberately the SAME callable for both proposer roles.
                proposer_breadth={"call_llm": "fake_proposer_roles_mod:breadth_fn"},
                proposer_depth={"call_llm": "fake_proposer_roles_mod:breadth_fn"},
            )
        },
        workspace_root=tmp_path,
    )
    # No error was raised, and both roles resolved to the identical object.
    assert cfg.proposer_breadth_call_llm is cfg.proposer_depth_call_llm
