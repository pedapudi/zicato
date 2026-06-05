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
