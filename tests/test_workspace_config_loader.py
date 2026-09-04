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
from pathlib import Path

import pytest

from zicato.workspace.config_io import WorkspaceConfig, read_workspace_config


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
            "runtime": {"parallelism": 8, "evaluation_model": "from-runtime"},
            "contract": {"board_path": "/live/board.jsonl"},
            "source_roots": ["src", "tools"],
            "generation_source_backend": "git",
            "adk_entrypoint": "pkg.mod:agent",
        },
    )
    config = read_workspace_config(root)
    assert config.exists is True
    assert config.runtime["parallelism"] == 8
    assert config.contract["board_path"] == "/live/board.jsonl"
    assert config.source_roots == ("src", "tools")
    assert config.generation_source_backend == "git"
    assert config.evaluation_model == "from-runtime"
    # Keys with no typed field stay reachable on the whole mapping.
    assert config.raw["adk_entrypoint"] == "pkg.mod:agent"


def test_a_top_level_evaluation_model_outranks_the_runtime_block(tmp_path: Path) -> None:
    root = _workspace(
        tmp_path,
        {"evaluation_model": "from-top-level", "runtime": {"evaluation_model": "from-runtime"}},
    )
    assert read_workspace_config(root).evaluation_model == "from-top-level"


def test_wrong_json_types_read_as_the_absent_key_default(tmp_path: Path) -> None:
    """A key of the wrong shape reads as absent rather than reaching a caller."""
    root = _workspace(
        tmp_path,
        {
            "runtime": ["not", "an", "object"],
            "contract": "not an object",
            "source_roots": "src",
            "generation_source_backend": 7,
        },
    )
    config = read_workspace_config(root)
    assert config.runtime == {}
    assert config.contract == {}
    assert config.source_roots == ()
    assert config.generation_source_backend == ""


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
