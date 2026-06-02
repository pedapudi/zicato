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
from zicato.mutation.validator import (
    check_forbidden_ids,
    validate_patches,
    validate_post_apply,
)


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

    import zicato

    pkg_root = Path(zicato.__file__).resolve().parent
    file_path = pkg_root / "cli" / "commands" / "mutations.py"
    spec = importlib.util.spec_from_file_location("zicato_mutations_cli_under_test", file_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.mutations_cmd


def test_validator_clean_run(tmp_path: Path) -> None:
    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    _write(
        src / "a.py",
        '''
        # zicato:mutable id="instr"
        INSTR = """initial"""
    ''',
    )
    pre = enumerate_mutations([src])
    patches = [_patch(pid="p1", mutation_id="instr", new_content='"""updated"""')]
    apply_patches(src, patches, tgt)
    errors = validate_post_apply(tgt, patches, pre)
    assert errors == []


def test_validator_detects_unresolved_id_post_apply(tmp_path: Path) -> None:
    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    _write(
        src / "a.py",
        '''
        # zicato:mutable id="instr"
        INSTR = """initial"""
    ''',
    )
    pre = enumerate_mutations([src])
    # Replacement strips the marker entirely (replaces the surrounding
    # span with content that no longer contains the marker comment).
    patches = [
        _patch(
            pid="p1",
            mutation_id="instr",
            new_content='"""no marker any more"""',
        )
    ]
    apply_patches(src, patches, tgt)
    # The marker still sits in the file (above the span). Hand-construct
    # an unresolved scenario by deleting the marker from the patched file
    # to simulate a rogue applier.
    target_file = tgt / "a.py"
    rewritten = target_file.read_text(encoding="utf-8").replace('# zicato:mutable id="instr"\n', "")
    target_file.write_text(rewritten, encoding="utf-8")

    errors = validate_post_apply(tgt, patches, pre)
    assert any("no longer resolves" in e for e in errors)


def test_validator_detects_unparseable_post_apply(tmp_path: Path) -> None:
    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    _write(
        src / "a.py",
        '''
        # zicato:mutable id="instr"
        INSTR = """initial"""
    ''',
    )
    pre = enumerate_mutations([src])
    # Inject a literal that breaks parsing once we paste it in.
    patches = [
        _patch(
            pid="p1",
            mutation_id="instr",
            new_content='"""unclosed',  # missing closing triple-quote
        )
    ]
    apply_patches(src, patches, tgt)
    errors = validate_post_apply(tgt, patches, pre)
    assert any("syntax error" in e.lower() for e in errors)


def test_validator_detects_missing_required_placeholder(tmp_path: Path) -> None:
    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    _write(
        src / "a.py",
        '''
        # zicato:mutable id="refine" required_placeholders="{drift_kind}"
        REFINE = """drift: {drift_kind}"""
    ''',
    )
    pre = enumerate_mutations([src])
    assert pre[0].metadata["required_placeholders"] == "{drift_kind}"
    # Replace content but drop the placeholder.
    patches = [
        _patch(
            pid="p1",
            mutation_id="refine",
            new_content='"""no placeholder here"""',
        )
    ]
    apply_patches(src, patches, tgt)
    errors = validate_post_apply(tgt, patches, pre)
    assert any("placeholder" in e for e in errors)


def test_validator_detects_dropped_imports(tmp_path: Path) -> None:
    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    _write(
        src / "a.py",
        '''
        import os
        from collections import OrderedDict

        # zicato:mutable:file id="all"
        """module-level"""
        VALUE = "x"
    ''',
    )
    pre = enumerate_mutations([src])
    # Replace the whole file, deleting the top-level imports.
    patches = [
        _patch(
            pid="p1",
            mutation_id="all",
            new_content=('# zicato:mutable:file id="all"\n' '"""module-level"""\n' 'VALUE = "y"\n'),
        )
    ]
    apply_patches(src, patches, tgt)
    errors = validate_post_apply(tgt, patches, pre)
    assert any("dropped top-level imports" in e for e in errors)


def test_validator_allows_added_imports(tmp_path: Path) -> None:
    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    _write(
        src / "a.py",
        '''
        import os

        # zicato:mutable:file id="all"
        """module-level"""
        VALUE = "x"
    ''',
    )
    pre = enumerate_mutations([src])
    patches = [
        _patch(
            pid="p1",
            mutation_id="all",
            new_content=(
                "import os\n"
                "import sys\n"
                "\n"
                '# zicato:mutable:file id="all"\n'
                '"""module-level"""\n'
                'VALUE = "y"\n'
            ),
        )
    ]
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


# ---------------------------------------------------------------------------
# validate_patches — deterministic atomic pre-validation.
# ---------------------------------------------------------------------------


def _full_patch(
    *,
    pid: str,
    mutation_id: str,
    op: str,
    new_content: str | None = None,
    new_numeric: float | None = None,
    new_enum: str | None = None,
) -> Patch:
    """A Patch builder that can populate any of the three payload fields."""

    return Patch(
        id=pid,
        mutation_id=mutation_id,
        op=op,  # type: ignore[arg-type]
        new_content=new_content,
        new_numeric=new_numeric,
        new_enum=new_enum,
        rationale="test",
    )


def _surface(tmp_path: Path) -> Path:
    """Write a tiny mutation surface: one span string point, one numeric
    span point, and one file-kind point."""

    src = tmp_path / "src"
    _write(
        src / "prompts.py",
        '''
        # zicato:mutable id="instr"
        INSTR = """initial"""

        # zicato:mutable id="threshold"
        THRESHOLD_DOC = "threshold"
        THRESHOLD = 0.5
    ''',
    )
    _write(
        src / "whole.py",
        '''
        # zicato:mutable:file id="whole_file"
        """module docstring"""
        VALUE = "x"
    ''',
    )
    return src


def test_validate_patches_requires_exactly_one_surface_source(
    tmp_path: Path,
) -> None:
    src = _surface(tmp_path)
    patch = _full_patch(pid="p1", mutation_id="instr", op="replace", new_content='"v"')
    # Neither supplied.
    with pytest.raises(ValueError, match="exactly one"):
        validate_patches([patch])
    # Both supplied.
    with pytest.raises(ValueError, match="exactly one"):
        validate_patches([patch], source_root=src, enumeration=enumerate_mutations([src]))


def test_validate_patches_clean_batch_returns_empty(tmp_path: Path) -> None:
    src = _surface(tmp_path)
    patches = [
        _full_patch(pid="p1", mutation_id="instr", op="replace", new_content='"v"'),
        _full_patch(pid="p2", mutation_id="threshold", op="set_numeric", new_numeric=0.9),
        _full_patch(
            pid="p3",
            mutation_id="whole_file",
            op="replace",
            new_content='"""x"""\nVALUE = "y"\n',
        ),
    ]
    # Both surface-source forms agree, and a clean batch is empty.
    assert validate_patches(patches, source_root=src) == []
    assert validate_patches(patches, enumeration=enumerate_mutations([src])) == []


def test_validate_patches_detects_unknown_mutation_id(tmp_path: Path) -> None:
    src = _surface(tmp_path)
    patches = [
        _full_patch(pid="p1", mutation_id="nope", op="replace", new_content='"v"'),
    ]
    errors = validate_patches(patches, source_root=src)
    assert len(errors) == 1
    assert "nope" in errors[0]
    assert "does not resolve" in errors[0]


def test_validate_patches_detects_op_payload_mismatch(tmp_path: Path) -> None:
    src = _surface(tmp_path)
    # replace with no new_content.
    missing = validate_patches(
        [_full_patch(pid="p1", mutation_id="instr", op="replace")],
        source_root=src,
    )
    assert any("requires new_content" in e for e in missing)

    # set_numeric with no new_numeric.
    missing_num = validate_patches(
        [_full_patch(pid="p2", mutation_id="threshold", op="set_numeric")],
        source_root=src,
    )
    assert any("requires new_numeric" in e for e in missing_num)

    # replace that also smuggles a foreign payload field (new_numeric).
    foreign = validate_patches(
        [
            _full_patch(
                pid="p3",
                mutation_id="instr",
                op="replace",
                new_content='"v"',
                new_numeric=1.0,
            )
        ],
        source_root=src,
    )
    assert any("must not set new_numeric" in e for e in foreign)


def test_validate_patches_detects_op_kind_mismatch(tmp_path: Path) -> None:
    src = _surface(tmp_path)
    # set_numeric against a file-kind point — no constant-after-marker
    # semantics apply to a whole-file point.
    errors = validate_patches(
        [
            _full_patch(
                pid="p1",
                mutation_id="whole_file",
                op="set_numeric",
                new_numeric=1.0,
            )
        ],
        source_root=src,
    )
    assert len(errors) == 1
    assert "incompatible" in errors[0]
    assert "file" in errors[0]


def test_validate_patches_detects_unknown_op(tmp_path: Path) -> None:
    src = _surface(tmp_path)
    errors = validate_patches(
        [_full_patch(pid="p1", mutation_id="instr", op="frobnicate")],
        source_root=src,
    )
    assert any("unknown op" in e for e in errors)


def test_validate_patches_reports_all_problems_not_just_first(
    tmp_path: Path,
) -> None:
    """A batch with one of EACH defect must surface ALL of them — the
    validator does not stop at the first problem."""

    src = _surface(tmp_path)
    patches = [
        # Defect 1: unknown mutation id.
        _full_patch(pid="bad_id", mutation_id="ghost", op="replace", new_content='"v"'),
        # Defect 2: op/payload mismatch (replace with no new_content).
        _full_patch(pid="bad_payload", mutation_id="instr", op="replace"),
        # Defect 3: op/kind mismatch (set_enum against a file point).
        _full_patch(pid="bad_kind", mutation_id="whole_file", op="set_enum", new_enum="x"),
        # A clean patch — must NOT produce an error.
        _full_patch(pid="ok", mutation_id="threshold", op="set_numeric", new_numeric=0.7),
    ]
    errors = validate_patches(patches, source_root=src)
    # All three defects are reported.
    assert any("ghost" in e and "does not resolve" in e for e in errors)
    assert any("bad_payload" in e and "requires new_content" in e for e in errors)
    assert any("bad_kind" in e and "incompatible" in e for e in errors)
    # The clean patch contributed nothing.
    assert not any("'ok'" in e for e in errors)


def test_validate_patches_empty_batch_is_clean(tmp_path: Path) -> None:
    src = _surface(tmp_path)
    assert validate_patches([], source_root=src) == []


def _code_surface(tmp_path: Path) -> Path:
    """A surface with one ``# zicato:mutable:code`` region."""
    src = tmp_path / "src"
    _write(
        src / "tools.py",
        """
        def slug(topic):
            # zicato:mutable:code id="slug_logic"
            s = topic.lower()
            # zicato:mutable:end
            return s
        """,
    )
    return src


def test_validate_patches_replace_accepts_code_point(tmp_path: Path) -> None:
    """``replace`` is compatible with a ``code``-kind point."""
    src = _code_surface(tmp_path)
    patches = [
        _full_patch(
            pid="p1",
            mutation_id="slug_logic",
            op="replace",
            new_content="    s = topic.strip().lower()\n",
        )
    ]
    assert validate_patches(patches, source_root=src) == []


def test_validate_patches_set_numeric_rejects_code_point(tmp_path: Path) -> None:
    """``set_numeric`` / ``set_enum`` require a ``span`` point — a code
    region has no constant-after-marker semantics."""
    src = _code_surface(tmp_path)
    errors = validate_patches(
        [_full_patch(pid="p1", mutation_id="slug_logic", op="set_numeric", new_numeric=1.0)],
        source_root=src,
    )
    assert len(errors) == 1
    assert "incompatible" in errors[0]
    assert "code" in errors[0]


def test_validate_post_apply_code_region_round_trips(tmp_path: Path) -> None:
    """A code-region replace survives the post-apply checks: the file
    still parses, the id still resolves, imports survive."""
    src = _code_surface(tmp_path)
    tgt = tmp_path / "tgt"
    pre = enumerate_mutations([src])
    patches = [
        _full_patch(
            pid="p1",
            mutation_id="slug_logic",
            op="replace",
            new_content="    s = topic.strip().lower().replace(' ', '-')\n",
        )
    ]
    apply_patches(src, patches, tgt)
    assert validate_post_apply(tgt, patches, pre) == []


def test_validate_patches_is_side_effect_free(tmp_path: Path) -> None:
    """Pre-validation must not touch the source tree."""

    src = _surface(tmp_path)
    before = (src / "prompts.py").read_text(encoding="utf-8")
    validate_patches(
        [_full_patch(pid="p1", mutation_id="instr", op="replace", new_content='"v"')],
        source_root=src,
    )
    assert (src / "prompts.py").read_text(encoding="utf-8") == before


@pytest.fixture
def workspace_with_harness(tmp_path: Path) -> Path:
    """Materialise a tiny inner-harness tree and a workspace pointing at it."""

    harness = tmp_path / "harness"
    _write(
        harness / "prompts.py",
        '''
        # zicato:mutable id="instr"
        INSTR = """You are an agent."""

        # zicato:mutable id="role" required_placeholders="{name}"
        ROLE = """Role: {name}"""
    ''',
    )
    _write(
        harness / "tools" / "router.py",
        '''
        # zicato:mutable:file id="router_prompts"
        """Prompts for the router."""

        ROUTE = "fast"
    ''',
    )

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
    result = runner.invoke(mutations_cmd, ["--workspace", str(workspace_with_harness)])
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
    result = runner.invoke(mutations_cmd, ["--workspace", str(workspace)])
    assert result.exit_code != 0
    assert "register" in result.output.lower()
