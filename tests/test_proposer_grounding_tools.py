"""Tests for the two grounding proposer tools: ``read_parent_diff`` +
``mutation_usage`` (``zicato.proposer.tools``).

``read_parent_diff`` is exercised against a REAL git-backed generation
store (seed → derive → diff the parent generation's tag against ITS
parent, read-only) and against the directory backend's journal-patch-
record fallback; the seed / missing-coordinate / oversized-diff edges are
pinned. ``mutation_usage`` is exercised against a fixture snapshot —
symbol + short-literal reference search through the existing
``grep_mutable`` machinery, so the mutable-subtree sandbox and match cap
apply by construction. Also covers the new read-only genstore methods the
diff tool rides on (``parent_generation_id`` / ``diff_generations``).
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from zicato.core.types import Patch
from zicato.epoch.git_genstore import GitGenerationStore
from zicato.epoch.journal import write_experiment
from zicato.proposer.tools import (
    ProposerToolContext,
    bind_proposer_tool_context,
    mutation_usage,
    read_parent_diff,
)
from zicato.testing import make_experiment, make_mutation_point, make_patch

# The git-backed halves drive real ``git`` subprocesses, matching the
# genstore suites' marking so the opt-in fast lane can skip them.
pytestmark = [pytest.mark.integration]


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")


def _mutable_tree(root: Path, *, instr: str = "original") -> Path:
    tree = root / "agent"
    _write(
        tree / "prompts.py",
        f'''
        # zicato:mutable id="instr"
        INSTR = """{instr}"""
        ''',
    )
    return tree


def _patch(pid: str, new_content: str) -> Patch:
    return Patch(
        id=pid,
        mutation_id="instr",
        op="replace",
        new_content=new_content,
        new_numeric=None,
        new_enum=None,
        rationale="test",
    )


def _ctx(
    workspace: Path,
    snapshot: Path,
    *,
    generation_id: str,
    mutations: tuple = (),
) -> ProposerToolContext:
    return ProposerToolContext(
        workspace_root=workspace,
        generation_root=snapshot,
        epoch_id="e1",
        mutations=mutations,
        generation_id=generation_id,
    )


# ---------------------------------------------------------------------------
# read_parent_diff — git backend
# ---------------------------------------------------------------------------


@pytest.fixture
def git_ws(tmp_path: Path) -> tuple[Path, GitGenerationStore]:
    """A git-backed workspace with ``e1/v0`` seeded and ``v1`` derived."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "config.json").write_text(
        json.dumps({"generation_source_backend": "git"}), encoding="utf-8"
    )
    store = GitGenerationStore(ws)
    store.seed_generation("e1", "v0", [_mutable_tree(tmp_path / "src")])
    patch = _patch("p1", "improved instruction")
    store.derive_generation("e1", "v0", "v1", [patch])
    write_experiment(
        ws,
        "e1",
        "v1",
        make_experiment(
            epoch_id="e1",
            generation_id="v1",
            parent_generation_id="v0",
            patches=(patch,),
        ),
    )
    return ws, store


@pytest.mark.slow
def test_genstore_parent_and_diff_read_surface(git_ws: tuple[Path, GitGenerationStore]) -> None:
    _, store = git_ws
    assert store.parent_generation_id("e1", "v1") == "v0"
    assert store.parent_generation_id("e1", "v0") is None  # seed
    diff = store.diff_generations("e1", "v0", "v1")
    assert '-INSTR = """original"""' in diff
    assert '+INSTR = """improved instruction"""' in diff
    with pytest.raises(FileNotFoundError):
        store.parent_generation_id("e1", "v9")
    with pytest.raises(FileNotFoundError):
        store.diff_generations("e1", "v0", "v9")


@pytest.mark.slow
def test_read_parent_diff_git_backend(git_ws: tuple[Path, GitGenerationStore]) -> None:
    ws, store = git_ws
    snapshot = store.materialize_snapshot("e1", "v1")
    with bind_proposer_tool_context(_ctx(ws, snapshot, generation_id="v1")):
        out = read_parent_diff()
    assert out.startswith("# diff v0 -> v1")
    assert "what the last promotion changed" in out
    assert '+INSTR = """improved instruction"""' in out
    assert '-INSTR = """original"""' in out


@pytest.mark.slow
def test_read_parent_diff_seed_generation(git_ws: tuple[Path, GitGenerationStore]) -> None:
    ws, store = git_ws
    snapshot = store.materialize_snapshot("e1", "v0")
    with bind_proposer_tool_context(_ctx(ws, snapshot, generation_id="v0")):
        out = read_parent_diff()
    assert "seed" in out
    assert "no prior promotion" in out


@pytest.mark.slow
def test_read_parent_diff_caps_output(git_ws: tuple[Path, GitGenerationStore]) -> None:
    ws, store = git_ws
    # A pathological promotion: replace the span with a huge body.
    patch = _patch("p2", "x" * 60_000)
    store.derive_generation("e1", "v1", "v2", [patch])
    write_experiment(
        ws,
        "e1",
        "v2",
        make_experiment(
            epoch_id="e1",
            generation_id="v2",
            parent_generation_id="v1",
            patches=(patch,),
        ),
    )
    snapshot = store.materialize_snapshot("e1", "v2")
    with bind_proposer_tool_context(_ctx(ws, snapshot, generation_id="v2")):
        out = read_parent_diff()
    assert len(out) < 30_000
    assert "truncated" in out


def test_read_parent_diff_without_coordinates(tmp_path: Path) -> None:
    with bind_proposer_tool_context(_ctx(tmp_path / "ws", tmp_path / "snap", generation_id="")):
        out = read_parent_diff()
    assert "coordinates unavailable" in out


# ---------------------------------------------------------------------------
# read_parent_diff — directory-backend fallback (journal patch records)
# ---------------------------------------------------------------------------


@pytest.fixture
def dir_ws(tmp_path: Path) -> Path:
    """A directory-backend workspace with ``v1``'s experiment journaled."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "config.json").write_text(
        json.dumps({"generation_source_backend": "directory"}), encoding="utf-8"
    )
    experiment = make_experiment(
        epoch_id="e1",
        generation_id="v1",
        parent_generation_id="v0",
        patches=(
            make_patch(
                id="p1",
                mutation_id="instr",
                new_content="improved instruction",
                rationale="tighten the span",
            ),
        ),
    )
    write_experiment(ws, "e1", "v1", experiment)
    return ws


def test_read_parent_diff_directory_backend_falls_back_to_patch_records(dir_ws: Path) -> None:
    with bind_proposer_tool_context(_ctx(dir_ws, dir_ws / "snap", generation_id="v1")):
        out = read_parent_diff()
    assert out.startswith("# patch record for v1")
    assert "journal patch records" in out
    assert "- replace instr: tighten the span" in out
    assert "improved instruction" in out


def test_read_parent_diff_directory_backend_seed(dir_ws: Path) -> None:
    with bind_proposer_tool_context(_ctx(dir_ws, dir_ws / "snap", generation_id="v0")):
        out = read_parent_diff()
    assert "no recorded patch set" in out
    assert "no prior promotion" in out


# ---------------------------------------------------------------------------
# mutation_usage
# ---------------------------------------------------------------------------


def _usage_snapshot(tmp_path: Path) -> tuple[Path, tuple]:
    """A snapshot where the point's symbol + value are referenced twice."""
    snapshot = tmp_path / "snapshot"
    harness = snapshot / "harness"
    harness.mkdir(parents=True)
    (harness / "prompts.py").write_text(
        "MAX_STEPS = 7\nSYSTEM_PROMPT = 'Be terse.'\n", encoding="utf-8"
    )
    (harness / "loop.py").write_text(
        "from prompts import MAX_STEPS\n\nfor _ in range(MAX_STEPS):\n    pass\n",
        encoding="utf-8",
    )
    point = make_mutation_point(
        id="harness__MAX_STEPS",
        kind="span",
        file=harness / "prompts.py",
        source_root=Path("/orig/harness"),
        content="7",
        metadata={"min": "1", "max": "20"},
    )
    return snapshot, (point,)


def test_mutation_usage_finds_symbol_and_value_references(tmp_path: Path) -> None:
    snapshot, mutations = _usage_snapshot(tmp_path)
    ctx = _ctx(tmp_path / "ws", snapshot, generation_id="v1", mutations=mutations)
    with bind_proposer_tool_context(ctx):
        out = mutation_usage("harness__MAX_STEPS")
    assert out.startswith("# usage of mutation point harness__MAX_STEPS")
    # Symbol references across BOTH files, via the sandboxed grep.
    assert "### references to 'MAX_STEPS'" in out
    assert "loop.py" in out and "prompts.py" in out
    # The short single-line value is searched too.
    assert "### references to '7'" in out


def test_mutation_usage_skips_long_multiline_content(tmp_path: Path) -> None:
    snapshot, _ = _usage_snapshot(tmp_path)
    long_point = make_mutation_point(
        id="harness__SYSTEM_PROMPT",
        file=snapshot / "harness" / "prompts.py",
        source_root=Path("/orig/harness"),
        content="line one\nline two — far too long to be a greppable literal",
    )
    ctx = _ctx(tmp_path / "ws", snapshot, generation_id="v1", mutations=(long_point,))
    with bind_proposer_tool_context(ctx):
        out = mutation_usage("harness__SYSTEM_PROMPT")
    # Only the symbol section renders — multi-line content is not grepped.
    assert "### references to 'SYSTEM_PROMPT'" in out
    assert out.count("### references to") == 1


def test_mutation_usage_stays_inside_the_sandbox(tmp_path: Path) -> None:
    """The search never leaves the mutable roots: a match planted OUTSIDE
    the snapshot is invisible."""
    snapshot, mutations = _usage_snapshot(tmp_path)
    (tmp_path / "outside.py").write_text("MAX_STEPS = 999\n", encoding="utf-8")
    ctx = _ctx(tmp_path / "ws", snapshot, generation_id="v1", mutations=mutations)
    with bind_proposer_tool_context(ctx):
        out = mutation_usage("harness__MAX_STEPS")
    assert "outside.py" not in out


def test_mutation_usage_rejects_unknown_ids(tmp_path: Path) -> None:
    snapshot, mutations = _usage_snapshot(tmp_path)
    ctx = _ctx(tmp_path / "ws", snapshot, generation_id="v1", mutations=mutations)
    with bind_proposer_tool_context(ctx):
        with pytest.raises(ValueError, match="unknown mutation id"):
            mutation_usage("not_in_manifest")


def test_grounding_tools_require_bound_context() -> None:
    with pytest.raises(RuntimeError, match="no bound ProposerToolContext"):
        read_parent_diff()
    with pytest.raises(RuntimeError, match="no bound ProposerToolContext"):
        mutation_usage("harness__MAX_STEPS")
