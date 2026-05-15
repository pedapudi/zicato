"""Tests for :mod:`zicato.adapter_factory`."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest

from zicato.adapter_factory import make_adapter_from_config


@pytest.fixture()
def fake_adk_module(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Install a fake :mod:`zicato.adapters.adk` so we don't need google-adk."""
    constructed: dict[str, Any] = {}

    class _FakeAdapter:
        name = "adk"

        def __init__(
            self,
            entrypoint: str,
            mutable_trees: list[Path] | None = None,
        ) -> None:
            constructed["entrypoint"] = entrypoint
            constructed["mutable_trees"] = mutable_trees

    fake_mod = types.ModuleType("zicato.adapters.adk")
    fake_mod.ADKHarnessAdapter = _FakeAdapter  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "zicato.adapters.adk", fake_mod)
    return constructed


def test_make_adapter_adk_uses_config_block(
    fake_adk_module: dict[str, Any],
) -> None:
    config = {
        "adapter": {
            "kind": "adk",
            "entrypoint": "my_pkg.agent:root",
            "mutable_trees": ["/tmp/tree_a", "/tmp/tree_b"],
        }
    }
    adapter = make_adapter_from_config(config)
    assert adapter.name == "adk"
    assert fake_adk_module["entrypoint"] == "my_pkg.agent:root"
    assert fake_adk_module["mutable_trees"] == [Path("/tmp/tree_a"), Path("/tmp/tree_b")]


def test_make_adapter_adk_without_mutable_trees(
    fake_adk_module: dict[str, Any],
) -> None:
    config = {
        "adapter": {
            "kind": "adk",
            "entrypoint": "my_pkg.agent:root",
        }
    }
    make_adapter_from_config(config)
    assert fake_adk_module["mutable_trees"] == []


def test_make_adapter_legacy_register_keys(
    fake_adk_module: dict[str, Any],
) -> None:
    """A config from `zicato register` (pre-factory) still works."""
    config = {
        "adk_entrypoint": "my_pkg.agent:root",
        "mutable_trees": ["/tmp/tree_a"],
    }
    make_adapter_from_config(config)
    assert fake_adk_module["entrypoint"] == "my_pkg.agent:root"
    assert fake_adk_module["mutable_trees"] == [Path("/tmp/tree_a")]


def test_make_adapter_missing_block_raises() -> None:
    with pytest.raises(ValueError, match="no 'adapter' block"):
        make_adapter_from_config({})


def test_make_adapter_unknown_kind_raises() -> None:
    with pytest.raises(ValueError, match="unknown adapter kind 'bogus'"):
        make_adapter_from_config({"adapter": {"kind": "bogus"}})


def test_make_adapter_missing_kind_raises() -> None:
    with pytest.raises(ValueError, match="must be a non-empty string"):
        make_adapter_from_config({"adapter": {"entrypoint": "a:b"}})


def test_make_adapter_adk_missing_entrypoint_raises(
    fake_adk_module: dict[str, Any],
) -> None:
    del fake_adk_module
    with pytest.raises(ValueError, match="non-empty 'entrypoint'"):
        make_adapter_from_config({"adapter": {"kind": "adk"}})


def test_make_adapter_block_must_be_mapping() -> None:
    with pytest.raises(ValueError, match="must be a mapping"):
        make_adapter_from_config({"adapter": ["not", "a", "mapping"]})
