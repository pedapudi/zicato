"""The workspace configuration loader: its absence rule, its typed fields.

:func:`zicato.workspace.config_io.read_workspace_config` is the only reader of
the workspace root's ``config.json``, so every rule the callers rely on is
pinned here rather than at each of them: an absent file reads as defaults, a
malformed one raises once, each block and key is normalized to its absent-key
default when the file holds the wrong JSON type, and
:meth:`~zicato.workspace.config_io.WorkspaceConfig.require` turns absence into
an error naming the remedy its caller chose.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from zicato.workspace.config_io import (
    WorkspaceConfig,
    read_workspace_config,
    write_workspace_config,
)


def _workspace(tmp_path: Path, config: object) -> Path:
    root = tmp_path / ".zicato"
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_text(json.dumps(config), encoding="utf-8")
    return root


def test_absent_file_reads_as_defaults(tmp_path: Path) -> None:
    """No config at all is a readable answer, not an exception."""
    config = read_workspace_config(tmp_path / "nowhere")
    assert config.exists is False
    assert config.path == tmp_path / "nowhere" / "config.json"
    assert config.raw == {}
    assert config.runtime == {}
    assert config.contract == {}
    assert config.source_roots == ()
    assert config.evaluation_model == ""
    assert config.generation_source_backend == ""


def test_absent_matches_the_reading_of_a_missing_file(tmp_path: Path) -> None:
    """The substitute a best-effort caller builds equals what the loader returns."""
    assert WorkspaceConfig.absent(tmp_path) == read_workspace_config(tmp_path)


def test_typed_fields_project_the_blocks_and_keys(tmp_path: Path) -> None:
    root = _workspace(
        tmp_path,
        {
            "runtime": {"parallelism": 8},
            "models": {"engines": {"evaluation": {"model": "configured-evaluator"}}},
            "contract": {"board_path": "/live/board.jsonl"},
            "generation_source_backend": "git",
            "adapter": {
                "kind": "adk",
                "entrypoint": "pkg.mod:agent",
                "mutable_trees": ["src", "tools"],
            },
        },
    )
    config = read_workspace_config(root)
    assert config.exists is True
    assert config.runtime["parallelism"] == 8
    assert config.contract["board_path"] == "/live/board.jsonl"
    assert config.source_roots == ("src", "tools")
    assert config.generation_source_backend == "git"
    assert config.evaluation_model == "configured-evaluator"
    # Keys with no typed field stay reachable on the whole mapping.
    assert config.raw["adapter"]["entrypoint"] == "pkg.mod:agent"


@pytest.mark.parametrize(
    "field",
    [
        "evaluation_model",
        "evaluation_call_llm",
        "adk_entrypoint",
        "source_roots",
        "mutable_trees",
        "harmonograf_url",
    ],
)
def test_workspace_rejects_alternate_registration_locations(tmp_path: Path, field: str) -> None:
    root = _workspace(tmp_path, {field: "obsolete"})
    with pytest.raises(ValueError, match=f"config.{field}: unknown field"):
        read_workspace_config(root)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("runtime", ["not", "an", "object"]),
        ("contract", "not an object"),
        ("adapter", "src"),
        ("generation_source_backend", 7),
    ],
)
def test_wrong_json_types_fail_at_the_authored_field(tmp_path: Path, field, value) -> None:
    root = _workspace(tmp_path, {field: value})
    with pytest.raises(ValueError, match=f"config.{field}:"):
        read_workspace_config(root)


def test_unparseable_json_raises_naming_the_path(tmp_path: Path) -> None:
    root = tmp_path / ".zicato"
    root.mkdir(parents=True)
    (root / "config.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="could not parse"):
        read_workspace_config(root)


def test_a_non_object_top_level_raises(tmp_path: Path) -> None:
    root = _workspace(tmp_path, ["a", "list"])
    with pytest.raises(ValueError, match="expected a JSON object at top level"):
        read_workspace_config(root)


def test_require_names_the_remedy_its_caller_chose(tmp_path: Path) -> None:
    config = read_workspace_config(tmp_path / "nowhere")
    with pytest.raises(FileNotFoundError, match="zicato init"):
        config.require()
    with pytest.raises(FileNotFoundError, match="zicato epoch register"):
        config.require("run `zicato epoch register` first")


def test_require_returns_a_config_that_is_there(tmp_path: Path) -> None:
    root = _workspace(tmp_path, {"instance_id": "test"})
    config = read_workspace_config(root)
    assert config.require() is config


def test_config_write_preserves_format_permissions_and_syncs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reference = tmp_path / "permissions.txt"
    reference.write_text("", encoding="utf-8")
    real_open, real_fsync = os.open, os.fsync
    creation_modes: list[int] = []
    synced: list[str] = []

    def observed_open(path: str, flags: int, mode: int = 0o777) -> int:
        if flags & os.O_CREAT:
            creation_modes.append(mode)
        return real_open(path, flags, mode)

    def observed_fsync(fd: int) -> None:
        synced.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        real_fsync(fd)

    monkeypatch.setattr(os, "open", observed_open)
    monkeypatch.setattr(os, "fsync", observed_fsync)
    write_workspace_config(tmp_path, {"instance_id": "é", "runtime": {"seed": 1}})

    target = tmp_path / "config.json"
    assert target.read_bytes() == (
        b'{\n  "instance_id": "\\u00e9",\n  "runtime": {\n    "seed": 1\n  }\n}\n'
    )
    assert creation_modes == [0o666]
    assert stat.S_IMODE(target.stat().st_mode) == stat.S_IMODE(reference.stat().st_mode)
    assert synced == ["file", "directory"]
    assert set(tmp_path.iterdir()) == {reference, target}


def test_config_write_requires_an_existing_workspace(tmp_path: Path) -> None:
    root = tmp_path / "missing"
    with pytest.raises(FileNotFoundError, match="does not exist"):
        write_workspace_config(root, {})
    assert not root.exists()
