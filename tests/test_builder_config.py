"""Tests for the builder ``builder.json`` config + secret-safety."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zicato.builder.config import (
    DEFAULT_SKILLS,
    BuilderConfig,
    load_builder_config,
)


def test_absent_builder_json_yields_defaults_and_chat_disabled(tmp_path: Path) -> None:
    cfg = load_builder_config(tmp_path)
    assert isinstance(cfg, BuilderConfig)
    assert cfg.agent.model == ""
    assert cfg.skills == DEFAULT_SKILLS
    assert cfg.theme is None
    assert cfg.chat_enabled is False


def test_model_present_enables_chat(tmp_path: Path) -> None:
    (tmp_path / "builder.json").write_text(
        json.dumps({"agent": {"model": "some-model"}}), encoding="utf-8"
    )
    cfg = load_builder_config(tmp_path)
    assert cfg.agent.model == "some-model"
    assert cfg.chat_enabled is True


def test_loads_from_dot_zicato_subdir(tmp_path: Path) -> None:
    zdir = tmp_path / ".zicato"
    zdir.mkdir()
    (zdir / "builder.json").write_text(
        json.dumps({"agent": {"model": "m"}, "theme": "dark"}), encoding="utf-8"
    )
    cfg = load_builder_config(tmp_path)
    assert cfg.agent.model == "m"
    assert cfg.theme == "dark"


def test_custom_skills_override_defaults(tmp_path: Path) -> None:
    (tmp_path / "builder.json").write_text(
        json.dumps({"skills": ["only-this-one"]}), encoding="utf-8"
    )
    cfg = load_builder_config(tmp_path)
    assert cfg.skills == ("only-this-one",)


def test_public_dict_carries_env_name_never_secret(tmp_path: Path) -> None:
    (tmp_path / "builder.json").write_text(
        json.dumps(
            {
                "agent": {
                    "model": "m",
                    "endpoint": "https://example.invalid",
                    "api_key_env": "MY_PROVIDER_KEY",
                    "call_llm": "pkg.mod:factory",
                }
            }
        ),
        encoding="utf-8",
    )
    cfg = load_builder_config(tmp_path)
    public = cfg.to_public_dict()
    # The env-var NAME is present...
    assert public["agent"]["api_key_env"] == "MY_PROVIDER_KEY"
    # ...and chat_enabled is folded in.
    assert public["chat_enabled"] is True
    # No field anywhere in the serialized form holds a resolved secret
    # value; the only key-ish field is the env-var name itself.
    blob = json.dumps(public)
    assert "MY_PROVIDER_KEY" in blob  # the name, fine
    # A real secret value would only appear if we resolved os.environ —
    # which we never do. Sanity: the structure has exactly the four
    # agent keys.
    assert set(public["agent"]) == {"model", "endpoint", "api_key_env", "call_llm"}


def test_empty_strings_collapse_to_none(tmp_path: Path) -> None:
    (tmp_path / "builder.json").write_text(
        json.dumps({"agent": {"endpoint": "", "api_key_env": ""}, "theme": ""}),
        encoding="utf-8",
    )
    cfg = load_builder_config(tmp_path)
    assert cfg.agent.endpoint is None
    assert cfg.agent.api_key_env is None
    assert cfg.theme is None


def test_malformed_json_raises(tmp_path: Path) -> None:
    (tmp_path / "builder.json").write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ValueError, match="expected a JSON object"):
        load_builder_config(tmp_path)
