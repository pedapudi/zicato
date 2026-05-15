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
