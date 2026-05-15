"""Tests for ``zicato.mutation.validator`` and the ``zicato mutations`` CLI."""

from __future__ import annotations

import importlib.util
import json
import sys
import textwrap
from pathlib import Path

import pytest
from click.testing import CliRunner

from zicato.core.types import Patch
from zicato.mutation.applier import apply_patches
from zicato.mutation.enumerator import enumerate_mutations
from zicato.mutation.validator import check_forbidden_ids, validate_post_apply


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")


def _patch(
    *,
    pid: str,
    mutation_id: str,
    op: str = "replace",
    new_content: str | None = None,
) -> Patch:
    return Patch(
        id=pid,
        mutation_id=mutation_id,
        op=op,  # type: ignore[arg-type]
        new_content=new_content,
        new_numeric=None,
        new_enum=None,
        rationale="test",
    )


def _load_mutations_command():
    """Load the standalone mutations command without relying on a
    ``zicato.cli`` package init that lives outside this agent's scope."""

    here = Path(__file__).resolve().parents[1]
    file_path = here / "zicato" / "cli" / "commands" / "mutations.py"
    spec = importlib.util.spec_from_file_location(
        "zicato_mutations_cli_under_test", file_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.mutations_cmd


def test_validator_clean_run(tmp_path: Path) -> None:
    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    _write(src / "a.py", '''
        # zicato:mutable id="instr"
        INSTR = """initial"""
    ''')
    pre = enumerate_mutations([src])
    patches = [_patch(pid="p1", mutation_id="instr", new_content='"""updated"""')]
    apply_patches(src, patches, tgt)
    errors = validate_post_apply(tgt, patches, pre)
    assert errors == []


def test_validator_detects_unresolved_id_post_apply(tmp_path: Path) -> None:
    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    _write(src / "a.py", '''
        # zicato:mutable id="instr"
        INSTR = """initial"""
    ''')
    pre = enumerate_mutations([src])
    # Replacement strips the marker entirely (replaces the surrounding
    # span with content that no longer contains the marker comment).
    patches = [_patch(
        pid="p1",
        mutation_id="instr",
        new_content='"""no marker any more"""',
    )]
    apply_patches(src, patches, tgt)
    # The marker still sits in the file (above the span). Hand-construct
    # an unresolved scenario by deleting the marker from the patched file
    # to simulate a rogue applier.
    target_file = tgt / "a.py"
    rewritten = target_file.read_text(encoding="utf-8").replace(
        '# zicato:mutable id="instr"\n', ""
    )
    target_file.write_text(rewritten, encoding="utf-8")

    errors = validate_post_apply(tgt, patches, pre)
    assert any("no longer resolves" in e for e in errors)


def test_validator_detects_unparseable_post_apply(tmp_path: Path) -> None:
    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    _write(src / "a.py", '''
        # zicato:mutable id="instr"
        INSTR = """initial"""
    ''')
    pre = enumerate_mutations([src])
    # Inject a literal that breaks parsing once we paste it in.
    patches = [_patch(
        pid="p1",
        mutation_id="instr",
        new_content='"""unclosed',  # missing closing triple-quote
    )]
    apply_patches(src, patches, tgt)
    errors = validate_post_apply(tgt, patches, pre)
    assert any("syntax error" in e.lower() for e in errors)


def test_validator_detects_missing_required_placeholder(tmp_path: Path) -> None:
    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    _write(src / "a.py", '''
        # zicato:mutable id="refine" required_placeholders="{drift_kind}"
        REFINE = """drift: {drift_kind}"""
    ''')
    pre = enumerate_mutations([src])
    assert pre[0].metadata["required_placeholders"] == "{drift_kind}"
    # Replace content but drop the placeholder.
    patches = [_patch(
        pid="p1",
        mutation_id="refine",
        new_content='"""no placeholder here"""',
    )]
    apply_patches(src, patches, tgt)
    errors = validate_post_apply(tgt, patches, pre)
    assert any("placeholder" in e for e in errors)


def test_validator_detects_dropped_imports(tmp_path: Path) -> None:
    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    _write(src / "a.py", '''
        import os
        from collections import OrderedDict

        # zicato:mutable:file id="all"
        """module-level"""
        VALUE = "x"
    ''')
    pre = enumerate_mutations([src])
    # Replace the whole file, deleting the top-level imports.
    patches = [_patch(
        pid="p1",
        mutation_id="all",
        new_content=(
            '# zicato:mutable:file id="all"\n'
            '"""module-level"""\n'
            'VALUE = "y"\n'
        ),
    )]
    apply_patches(src, patches, tgt)
    errors = validate_post_apply(tgt, patches, pre)
    assert any("dropped top-level imports" in e for e in errors)


def test_validator_allows_added_imports(tmp_path: Path) -> None:
    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    _write(src / "a.py", '''
        import os

        # zicato:mutable:file id="all"
        """module-level"""
        VALUE = "x"
    ''')
    pre = enumerate_mutations([src])
    patches = [_patch(
        pid="p1",
        mutation_id="all",
        new_content=(
            'import os\n'
            'import sys\n'
            '\n'
            '# zicato:mutable:file id="all"\n'
            '"""module-level"""\n'
            'VALUE = "y"\n'
        ),
    )]
    apply_patches(src, patches, tgt)
    errors = validate_post_apply(tgt, patches, pre)
    assert errors == []


def test_check_forbidden_ids() -> None:
    patches = [
        _patch(pid="p1", mutation_id="safe_one", new_content='"v"'),
        _patch(pid="p2", mutation_id="locked", new_content='"v"'),
    ]
    errors = check_forbidden_ids(patches, ["locked", "also_locked"])
    assert len(errors) == 1
    assert "locked" in errors[0]


def test_check_forbidden_ids_empty() -> None:
    assert check_forbidden_ids([], ["locked"]) == []


@pytest.fixture
def workspace_with_harness(tmp_path: Path) -> Path:
    """Materialise a tiny inner-harness tree and a workspace pointing at it."""

    harness = tmp_path / "harness"
    _write(harness / "prompts.py", '''
        # zicato:mutable id="instr"
        INSTR = """You are an agent."""

        # zicato:mutable id="role" required_placeholders="{name}"
        ROLE = """Role: {name}"""
    ''')
    _write(harness / "tools" / "router.py", '''
        # zicato:mutable:file id="router_prompts"
        """Prompts for the router."""

        ROUTE = "fast"
    ''')

    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    (workspace / "config.json").write_text(
        json.dumps({"source_roots": [str(harness)]}),
        encoding="utf-8",
    )
    return workspace


def test_cli_table_output(workspace_with_harness: Path) -> None:
    mutations_cmd = _load_mutations_command()
    runner = CliRunner()
    result = runner.invoke(
        mutations_cmd, ["--workspace", str(workspace_with_harness)]
    )
    assert result.exit_code == 0, result.output
    assert "instr" in result.output
    assert "role" in result.output
    assert "router_prompts" in result.output
    assert "Total:" in result.output
    assert "span=" in result.output and "file=" in result.output


def test_cli_json_output(workspace_with_harness: Path) -> None:
    mutations_cmd = _load_mutations_command()
    runner = CliRunner()
    result = runner.invoke(
        mutations_cmd,
        ["--workspace", str(workspace_with_harness), "--format", "json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "points" in payload
    assert "summary" in payload
    assert payload["summary"]["total"] == 3
    ids = {p["id"] for p in payload["points"]}
    assert ids == {"instr", "role", "router_prompts"}


def test_cli_filters_by_id_glob(workspace_with_harness: Path) -> None:
    mutations_cmd = _load_mutations_command()
    runner = CliRunner()
    result = runner.invoke(
        mutations_cmd,
        [
            "--workspace",
            str(workspace_with_harness),
            "--id",
            "router_*",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    ids = {p["id"] for p in payload["points"]}
    assert ids == {"router_prompts"}


def test_cli_filters_by_kind(workspace_with_harness: Path) -> None:
    mutations_cmd = _load_mutations_command()
    runner = CliRunner()
    result = runner.invoke(
        mutations_cmd,
        [
            "--workspace",
            str(workspace_with_harness),
            "--kind",
            "file",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert {p["kind"] for p in payload["points"]} == {"file"}


def test_cli_show_full(workspace_with_harness: Path) -> None:
    mutations_cmd = _load_mutations_command()
    runner = CliRunner()
    result = runner.invoke(
        mutations_cmd,
        [
            "--workspace",
            str(workspace_with_harness),
            "--show",
            "full",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    for item in payload["points"]:
        assert "content" in item
        assert "preview" not in item


def test_cli_missing_config_errors(tmp_path: Path) -> None:
    mutations_cmd = _load_mutations_command()
    runner = CliRunner()
    workspace = tmp_path / "empty_workspace"
    workspace.mkdir()
    result = runner.invoke(
        mutations_cmd, ["--workspace", str(workspace)]
    )
    assert result.exit_code != 0
    assert "register" in result.output.lower()
