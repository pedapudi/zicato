"""Tests for the proposer's snapshot reads (``zicato.proposer.tools``).

``mutation_usage`` is the host tool an episode is served; ``grep_mutable``
is the sandboxed search it is built from. Both are exercised against a
fixture generation-root snapshot plus a mutation manifest: the returned
content, that the search stays inside the snapshot and never writes, that
each raises cleanly with no bound context, and that the bind
context-manager sets AND resets the module-level context var.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zicato.proposer import tool_context
from zicato.proposer import tools as proposer_tools
from zicato.proposer.tool_context import ProposerToolContext, bind_proposer_tool_context
from zicato.proposer.tools import grep_mutable, mutation_usage
from zicato.testing import make_mutation_point


def _build_snapshot(tmp_path: Path) -> tuple[Path, tuple]:
    """Build a generation snapshot + a manifest re-basing onto it.

    Layout::

        {tmp}/snapshot/harness/prompts.py
        {tmp}/snapshot/harness/router.py
    """
    snapshot = tmp_path / "snapshot"
    harness = snapshot / "harness"
    harness.mkdir(parents=True)
    (harness / "prompts.py").write_text(
        "SYSTEM_PROMPT = 'You are a helpful assistant.'\n", encoding="utf-8"
    )
    (harness / "router.py").write_text(
        "def route(msg):\n    return 'fallback'  # TODO tighten\n", encoding="utf-8"
    )

    mp = make_mutation_point(
        id="harness__system_prompt",
        file=harness / "prompts.py",
        source_root=Path("/orig/harness"),
        content="You are a helpful assistant.",
    )
    return snapshot, (mp,)


def _make_ctx(tmp_path: Path) -> ProposerToolContext:
    snapshot, mutations = _build_snapshot(tmp_path)
    return ProposerToolContext(
        workspace_root=tmp_path / "ws",
        generation_root=snapshot,
        epoch_id="ep-001",
        mutations=mutations,
    )


# ---------------------------------------------------------------------------
# grep_mutable
# ---------------------------------------------------------------------------


def test_grep_mutable_finds_matches(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    with bind_proposer_tool_context(ctx):
        out = grep_mutable(r"TODO")
    assert "harness/router.py:" in out
    assert "TODO tighten" in out


def test_grep_mutable_no_matches_signal(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    with bind_proposer_tool_context(ctx):
        out = grep_mutable(r"zzz_no_such_token_zzz")
    assert out == "(no matches)"


def test_grep_mutable_invalid_regex_raises(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    with bind_proposer_tool_context(ctx):
        with pytest.raises(ValueError, match="invalid regex"):
            grep_mutable(r"(unterminated")


def test_grep_mutable_never_writes(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    snapshot = tmp_path / "snapshot"
    before = {p: p.read_bytes() for p in snapshot.rglob("*") if p.is_file()}
    with bind_proposer_tool_context(ctx):
        grep_mutable(r".")
    for path, data in before.items():
        assert path.read_bytes() == data


def test_grep_mutable_annotates_its_match_cap(tmp_path: Path) -> None:
    """Past the cap the result says so, rather than reading as complete."""
    ctx = _make_ctx(tmp_path)
    harness = tmp_path / "snapshot" / "harness"
    (harness / "prompts.py").write_text("HIT = 1\nHIT = 2\nHIT = 3\nHIT = 4\n", encoding="utf-8")
    with bind_proposer_tool_context(ctx):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(proposer_tools, "_GREP_MATCH_LIMIT", 2)
            out = grep_mutable(r"HIT")

    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) == 3  # two matches plus the truncation note
    assert lines[-1].startswith("[... truncated:")


def test_grep_mutable_reads_the_whole_snapshot_not_only_the_mutable_subtree(
    tmp_path: Path,
) -> None:
    """The searchable surface is the generation root, subtrees included.

    Where an adapter declares a narrower mutable subtree, the non-mutable
    code that CONSUMES a mutable value still falls inside the searched tree.
    Here ``runner.py`` sits in the snapshot but outside the declared
    ``agent`` subtree, and it is the reference that tells the proposer who
    reads the value it is about to rewrite. Every match appears once, under
    its snapshot-relative path.
    """
    snapshot = tmp_path / "generations" / "v1" / "snapshot"
    agent = snapshot / "agent"
    agent.mkdir(parents=True)
    (agent / "prompts.py").write_text(
        "SYSTEM_PROMPT = 'You are a helpful assistant.'\n", encoding="utf-8"
    )
    (snapshot / "runner.py").write_text(
        "from agent.prompts import SYSTEM_PROMPT\n", encoding="utf-8"
    )
    point = make_mutation_point(
        id="agent__system_prompt",
        file=agent / "prompts.py",
        source_root=agent,
        content="You are a helpful assistant.",
    )
    ctx = ProposerToolContext(
        workspace_root=tmp_path / "ws",
        generation_root=snapshot,
        epoch_id="ep-001",
        mutations=(point,),
    )

    with bind_proposer_tool_context(ctx):
        out = grep_mutable(r"SYSTEM_PROMPT")

    assert [line for line in out.splitlines() if line.strip()] == [
        "agent/prompts.py:1: SYSTEM_PROMPT = 'You are a helpful assistant.'",
        "runner.py:1: from agent.prompts import SYSTEM_PROMPT",
    ]


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


def _usage_ctx(tmp_path: Path, snapshot: Path, mutations: tuple) -> ProposerToolContext:
    return ProposerToolContext(
        workspace_root=tmp_path / "ws",
        generation_root=snapshot,
        epoch_id="e1",
        mutations=mutations,
        generation_id="v1",
    )


def test_mutation_usage_finds_symbol_and_value_references(tmp_path: Path) -> None:
    snapshot, mutations = _usage_snapshot(tmp_path)
    with bind_proposer_tool_context(_usage_ctx(tmp_path, snapshot, mutations)):
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
    with bind_proposer_tool_context(_usage_ctx(tmp_path, snapshot, (long_point,))):
        out = mutation_usage("harness__SYSTEM_PROMPT")
    # Only the symbol section renders — multi-line content is not grepped.
    assert "### references to 'SYSTEM_PROMPT'" in out
    assert out.count("### references to") == 1


def test_mutation_usage_stays_inside_the_sandbox(tmp_path: Path) -> None:
    """The search never leaves the snapshot: a match planted OUTSIDE it is
    invisible."""
    snapshot, mutations = _usage_snapshot(tmp_path)
    (tmp_path / "outside.py").write_text("MAX_STEPS = 999\n", encoding="utf-8")
    with bind_proposer_tool_context(_usage_ctx(tmp_path, snapshot, mutations)):
        out = mutation_usage("harness__MAX_STEPS")
    assert "outside.py" not in out


def test_mutation_usage_rejects_unknown_ids(tmp_path: Path) -> None:
    snapshot, mutations = _usage_snapshot(tmp_path)
    with bind_proposer_tool_context(_usage_ctx(tmp_path, snapshot, mutations)):
        with pytest.raises(ValueError, match="unknown mutation id"):
            mutation_usage("not_in_manifest")


# ---------------------------------------------------------------------------
# the bound context
# ---------------------------------------------------------------------------


def test_tools_raise_with_no_bound_context() -> None:
    with pytest.raises(RuntimeError, match="no bound ProposerToolContext"):
        grep_mutable(r".")
    with pytest.raises(RuntimeError, match="no bound ProposerToolContext"):
        mutation_usage("harness__MAX_STEPS")


def test_bind_context_sets_and_resets(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    # Unset before.
    assert tool_context._TOOL_CONTEXT.get() is None
    with bind_proposer_tool_context(ctx):
        assert tool_context._TOOL_CONTEXT.get() is ctx
    # Reset after the block, even though we entered cleanly.
    assert tool_context._TOOL_CONTEXT.get() is None


def test_bind_context_resets_on_exception(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    with pytest.raises(RuntimeError, match="boom"):
        with bind_proposer_tool_context(ctx):
            raise RuntimeError("boom")
    assert tool_context._TOOL_CONTEXT.get() is None
