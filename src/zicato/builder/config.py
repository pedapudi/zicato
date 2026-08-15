"""Load builder-only skills and theme settings from ``builder.json``.

Model routing belongs exclusively to the workspace ``models.builder`` role.
Keeping it out of this file prevents two configuration sources from drifting.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_SKILLS = ("zicato-build-tournament", "zicato-build-board")


@dataclass(frozen=True, slots=True)
class BuilderConfig:
    skills: tuple[str, ...] = DEFAULT_SKILLS
    theme: str | None = None

    def to_public_dict(self) -> dict[str, Any]:
        return {"skills": list(self.skills), "theme": self.theme}


def _builder_config_path(root: Path) -> Path | None:
    for path in (root / "builder.json", root / ".zicato" / "builder.json"):
        if path.is_file():
            return path
    return None


def load_builder_config(workspace_root: Path) -> BuilderConfig:
    """Load builder presentation settings, defaulting absent fields."""
    path = _builder_config_path(Path(workspace_root))
    if path is None:
        return BuilderConfig()
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"could not parse {path}: {exc.msg}") from exc
    if not isinstance(loaded, Mapping):
        raise ValueError(f"{path}: expected a JSON object at top level")
    raw_skills = loaded.get("skills")
    skills = (
        tuple(str(skill) for skill in raw_skills)
        if isinstance(raw_skills, list | tuple) and raw_skills
        else DEFAULT_SKILLS
    )
    theme = loaded.get("theme")
    return BuilderConfig(skills=skills, theme=str(theme) if theme else None)


__all__ = ["DEFAULT_SKILLS", "BuilderConfig", "load_builder_config"]
