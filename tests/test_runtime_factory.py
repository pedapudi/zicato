"""Tests for :mod:`zicato.runtime_factory`."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from zicato.runtime_factory import make_runtime_config


async def _stub_harness(system: str, user: str, model: str) -> str:
    del system, user, model
    return ""


async def _stub_aux(system: str, user: str, model: str) -> str:
    del system, user, model
    return ""


def test_runtime_config_from_explicit_callables(tmp_path: Path) -> None:
    cfg = make_runtime_config(
        {"runtime": {"instance_id": "smoke"}},
        workspace_root=tmp_path,
        harness_call_llm=_stub_harness,
        auxiliary_call_llm=_stub_aux,
    )
    assert cfg.instance_id == "smoke"
    assert cfg.workspace_root == tmp_path
    assert cfg.harness_call_llm is _stub_harness
    assert cfg.auxiliary_call_llm is _stub_aux


def test_runtime_config_seed_passes_through(tmp_path: Path) -> None:
    cfg = make_runtime_config(
        {"runtime": {"seed": 1234}},
        workspace_root=tmp_path,
        harness_call_llm=_stub_harness,
        auxiliary_call_llm=_stub_aux,
    )
    assert cfg.seed == 1234


def test_runtime_config_scrub_worker_env_defaults_off(tmp_path: Path) -> None:
    """Absent ``runtime.scrub_worker_env`` ⇒ off (today's full-inheritance)."""
    cfg = make_runtime_config(
        {"runtime": {}},
        workspace_root=tmp_path,
        harness_call_llm=_stub_harness,
        auxiliary_call_llm=_stub_aux,
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
        harness_call_llm=_stub_harness,
        auxiliary_call_llm=_stub_aux,
    )
    assert cfg.scrub_worker_env is True
    assert cfg.worker_env_passthrough == ("CUSTOM_A", "CUSTOM_B")


def test_runtime_config_diversity_tolerance_defaults_off(tmp_path: Path) -> None:
    """Absent ``runtime.diversity_tolerance`` ⇒ ``None`` (enforcement off)."""
    cfg = make_runtime_config(
        {"runtime": {}},
        workspace_root=tmp_path,
        harness_call_llm=_stub_harness,
        auxiliary_call_llm=_stub_aux,
    )
    assert cfg.diversity_tolerance is None


def test_runtime_config_diversity_tolerance_opt_in(tmp_path: Path) -> None:
    """``runtime.diversity_tolerance`` is read into the config as a float."""
    cfg = make_runtime_config(
        {"runtime": {"diversity_tolerance": 0.5}},
        workspace_root=tmp_path,
        harness_call_llm=_stub_harness,
        auxiliary_call_llm=_stub_aux,
    )
    assert cfg.diversity_tolerance == 0.5


def test_runtime_config_diversity_tolerance_out_of_range_raises(tmp_path: Path) -> None:
    """An out-of-(0, 1] tolerance is rejected at construction."""
    with pytest.raises(ValueError, match="diversity_tolerance"):
        make_runtime_config(
            {"runtime": {"diversity_tolerance": 1.5}},
            workspace_root=tmp_path,
            harness_call_llm=_stub_harness,
            auxiliary_call_llm=_stub_aux,
        )


def test_runtime_config_parallelism_workspace_value_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An explicit ``runtime.parallelism`` in the workspace config wins.

    It must override even an env-backed ``ZICATO_PARALLELISM`` value.
    """
    monkeypatch.setenv("ZICATO_PARALLELISM", "7")
    cfg = make_runtime_config(
        {"runtime": {"parallelism": 3}},
        workspace_root=tmp_path,
        harness_call_llm=_stub_harness,
        auxiliary_call_llm=_stub_aux,
    )
    assert cfg.parallelism == 3


def test_runtime_config_parallelism_env_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With no workspace value, the env-backed typed config supplies it."""
    monkeypatch.setenv("ZICATO_PARALLELISM", "9")
    cfg = make_runtime_config(
        {"runtime": {}},
        workspace_root=tmp_path,
        harness_call_llm=_stub_harness,
        auxiliary_call_llm=_stub_aux,
    )
    assert cfg.parallelism == 9


def test_runtime_config_parallelism_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With neither a workspace value nor an env var, the default 4 holds."""
    monkeypatch.delenv("ZICATO_PARALLELISM", raising=False)
    cfg = make_runtime_config(
        {},
        workspace_root=tmp_path,
        harness_call_llm=_stub_harness,
        auxiliary_call_llm=_stub_aux,
    )
    assert cfg.parallelism == 4


def test_runtime_config_default_instance_id(tmp_path: Path) -> None:
    cfg = make_runtime_config(
        {},
        workspace_root=tmp_path,
        harness_call_llm=_stub_harness,
        auxiliary_call_llm=_stub_aux,
    )
    assert cfg.instance_id == "default"


def test_runtime_config_rejects_shared_callable(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="must be distinct"):
        make_runtime_config(
            {},
            workspace_root=tmp_path,
            harness_call_llm=_stub_harness,
            auxiliary_call_llm=_stub_harness,
        )


def test_runtime_config_resolves_dotted_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_mod = types.ModuleType("fake_runtime_mod")
    fake_mod.harness_fn = _stub_harness  # type: ignore[attr-defined]
    fake_mod.aux_fn = _stub_aux  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fake_runtime_mod", fake_mod)

    cfg = make_runtime_config(
        {
            "runtime": {
                "harness_call_llm": "fake_runtime_mod.harness_fn",
                "auxiliary_call_llm": "fake_runtime_mod:aux_fn",
            }
        },
        workspace_root=tmp_path,
    )
    assert cfg.harness_call_llm is _stub_harness
    assert cfg.auxiliary_call_llm is _stub_aux


def test_runtime_config_missing_harness_dotted_path_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="harness_call_llm"):
        make_runtime_config({}, workspace_root=tmp_path)


def test_runtime_config_missing_aux_dotted_path_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="auxiliary_call_llm"):
        make_runtime_config(
            {},
            workspace_root=tmp_path,
            harness_call_llm=_stub_harness,
        )


def test_runtime_config_bad_dotted_path_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="could not import"):
        make_runtime_config(
            {
                "runtime": {
                    "harness_call_llm": "nope_module.attr",
                    "auxiliary_call_llm": "nope_module.attr_other",
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
                    "harness_call_llm": "noncallable_mod.value",
                    "auxiliary_call_llm": "noncallable_mod.value",
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
    mod.harness_fn = _stub_harness  # type: ignore[attr-defined]
    mod.aux_fn = _stub_aux  # type: ignore[attr-defined]

    async def _judge(system: str, user: str, model: str) -> str:
        del system, user, model
        return ""

    mod.judge_fn = _judge  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fake_models_mod", mod)


def test_models_block_resolves_harness_and_auxiliary_dotted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``models.{harness,auxiliary}`` (dotted form) wins over ``runtime.*``."""
    _install_models_callables(monkeypatch)
    cfg = make_runtime_config(
        {
            # legacy runtime.* present but should be ignored when models.* set.
            "runtime": {
                "harness_call_llm": "fake_models_mod.judge_fn",
                "auxiliary_call_llm": "fake_models_mod.judge_fn",
            },
            "models": {
                "harness": {"call_llm": "fake_models_mod:harness_fn"},
                "auxiliary": {"call_llm": "fake_models_mod:aux_fn"},
            },
        },
        workspace_root=tmp_path,
    )
    assert cfg.harness_call_llm is _stub_harness
    assert cfg.auxiliary_call_llm is _stub_aux


def test_models_block_falls_back_to_runtime_when_role_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unconfigured ``models`` role falls back to ``runtime.*`` (today's path)."""
    _install_models_callables(monkeypatch)
    cfg = make_runtime_config(
        {
            "runtime": {
                "harness_call_llm": "fake_models_mod.harness_fn",
                "auxiliary_call_llm": "fake_models_mod.aux_fn",
            },
            # empty models block ⇒ both roles fall through to runtime.*
            "models": {},
        },
        workspace_root=tmp_path,
    )
    assert cfg.harness_call_llm is _stub_harness
    assert cfg.auxiliary_call_llm is _stub_aux
    # No judge role ⇒ judge_call_llm is None and judges use the auxiliary.
    assert cfg.judge_call_llm is None
    assert cfg.effective_judge_call_llm() is _stub_aux


def test_explicit_callable_kwarg_beats_models_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An explicit callable kwarg wins over a configured ``models`` role."""
    _install_models_callables(monkeypatch)
    cfg = make_runtime_config(
        {"models": {"harness": {"call_llm": "fake_models_mod:harness_fn"}}},
        workspace_root=tmp_path,
        harness_call_llm=_stub_aux,  # the kwarg wins
        auxiliary_call_llm=_stub_harness,
    )
    assert cfg.harness_call_llm is _stub_aux


def test_models_harness_endpoint_spec_builds_inner_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A ``models.harness`` *model spec* with an endpoint builds the inner ADK
    model so the adapter can rebind the target's agents to it (function-calling
    against the configured endpoint), not the text-only shim."""
    pytest.importorskip("litellm")
    from google.adk.models.lite_llm import LiteLlm

    _install_models_callables(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-dummy")
    cfg = make_runtime_config(
        {
            "models": {
                "harness": {
                    "model": "openai/gemma-4-26B-A4B-it-FP8",
                    "endpoint": "http://kossel.lan:8080/v1",
                    "api_key_env": "OPENAI_API_KEY",
                },
                "auxiliary": {"call_llm": "fake_models_mod:aux_fn"},
            }
        },
        workspace_root=tmp_path,
    )
    assert isinstance(cfg.inner_model, LiteLlm)


def test_dotted_harness_role_leaves_inner_model_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A dotted ``call_llm`` harness role configures no inner model — the
    adapter falls back to its guarded shim rebind (today's behaviour)."""
    _install_models_callables(monkeypatch)
    cfg = make_runtime_config(
        {
            "models": {
                "harness": {"call_llm": "fake_models_mod:harness_fn"},
                "auxiliary": {"call_llm": "fake_models_mod:aux_fn"},
            }
        },
        workspace_root=tmp_path,
    )
    assert cfg.inner_model is None


def test_models_judge_role_resolves_and_overrides_auxiliary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``models.judge`` resolves to a distinct judge callable; else aux is used."""
    _install_models_callables(monkeypatch)
    cfg = make_runtime_config(
        {
            "models": {
                "harness": {"call_llm": "fake_models_mod:harness_fn"},
                "auxiliary": {"call_llm": "fake_models_mod:aux_fn"},
                "judge": {"call_llm": "fake_models_mod:judge_fn"},
            }
        },
        workspace_root=tmp_path,
    )
    assert cfg.judge_call_llm is not None
    # The judge callable is the configured one, NOT the auxiliary.
    assert cfg.effective_judge_call_llm() is cfg.judge_call_llm
    assert cfg.effective_judge_call_llm() is not cfg.auxiliary_call_llm


def test_models_collusion_guard_fires_when_harness_equals_auxiliary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The collusion guard still rejects harness==auxiliary via the models block."""
    _install_models_callables(monkeypatch)
    with pytest.raises(RuntimeError, match="must be distinct"):
        make_runtime_config(
            {
                "models": {
                    "harness": {"call_llm": "fake_models_mod:harness_fn"},
                    "auxiliary": {"call_llm": "fake_models_mod:harness_fn"},
                }
            },
            workspace_root=tmp_path,
        )
