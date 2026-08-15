"""Tests for builder-only presentation config."""

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
    assert cfg.skills == DEFAULT_SKILLS
    assert cfg.theme is None


def test_loads_from_dot_zicato_subdir(tmp_path: Path) -> None:
    zdir = tmp_path / ".zicato"
    zdir.mkdir()
    (zdir / "builder.json").write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
    cfg = load_builder_config(tmp_path)
    assert cfg.theme == "dark"


def test_custom_skills_override_defaults(tmp_path: Path) -> None:
    (tmp_path / "builder.json").write_text(
        json.dumps({"skills": ["only-this-one"]}), encoding="utf-8"
    )
    cfg = load_builder_config(tmp_path)
    assert cfg.skills == ("only-this-one",)


def test_public_dict_contains_only_builder_presentation(tmp_path: Path) -> None:
    (tmp_path / "builder.json").write_text(
        json.dumps({"agent": {"model": "ignored"}, "theme": ""}),
        encoding="utf-8",
    )
    cfg = load_builder_config(tmp_path)
    assert cfg.theme is None
    assert cfg.to_public_dict() == {"skills": list(DEFAULT_SKILLS), "theme": None}


def test_malformed_json_raises(tmp_path: Path) -> None:
    (tmp_path / "builder.json").write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ValueError, match="expected a JSON object"):
        load_builder_config(tmp_path)
