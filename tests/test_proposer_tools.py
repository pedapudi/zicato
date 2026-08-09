"""Tests for the read-only proposer tool registry (``zicato.proposer.tools``).

Each tool is exercised against a fixture generation-root snapshot plus a
mutation manifest whose ``source_root`` basenames re-base onto that
snapshot. The tests assert the returned content, that the read / grep
tools refuse path traversal and never write, that every tool raises
cleanly with no bound context, and that the bind context-manager sets AND
resets the module-level context var.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zicato.proposer import tools as proposer_tools
from zicato.proposer.tools import (
    DEFAULT_PROPOSER_TOOLS,
    ProposerToolContext,
    bind_proposer_tool_context,
    grep_mutable,
    list_mutation_points,
    mutation_track_record,
    mutation_usage,
    read_insights,
    read_journal,
    read_mutable_file,
    read_parent_diff,
)
from zicato.testing import make_mutation_point


def _build_snapshot(tmp_path: Path) -> tuple[Path, tuple]:
    """Build a generation snapshot + a manifest re-basing onto it.

    Layout::

        {tmp}/snapshot/harness/prompts.py
        {tmp}/snapshot/harness/router.py

    The manifest's ``source_root`` basename is ``harness``, so
    ``ProposerToolContext.mutable_roots`` resolves ``{snapshot}/harness``.
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


def test_list_mutation_points_renders_manifest(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    with bind_proposer_tool_context(ctx):
        payload = json.loads(list_mutation_points())
    entries = payload["mutation_points"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["id"] == "harness__system_prompt"
    assert entry["content"] == "You are a helpful assistant."
    # File is rendered relative to the snapshot root when possible.
    assert entry["file"] == "harness/prompts.py"


def test_read_mutable_file_returns_content(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    with bind_proposer_tool_context(ctx):
        text = read_mutable_file("prompts.py")
    assert "You are a helpful assistant." in text


def _build_declared_subtree_snapshot(tmp_path: Path) -> tuple[Path, tuple]:
    """A generation snapshot whose adapter declares a NARROWER mutable subtree.

    Layout — the mutable tree lives under ``agent/`` inside the snapshot,
    exactly as a real generation snapshot copies a registered ``agent`` tree
    under its basename::

        {tmp}/generations/v1/snapshot/agent/prompts.py   <- mutable, declared
        {tmp}/generations/v1/snapshot/runner.py          <- NOT mutable, consumes it

    The manifest's ``source_root`` is the DECLARED mutable subtree
    (``{snapshot}/agent``) — what an adapter with a ``mutable_subpaths``
    declaration enumerates from. This is the case the issue #20 regression
    bit: the old derivation admitted ONLY that narrow subtree as a readable
    root, while :func:`list_mutation_points` advertises the file
    snapshot-relative (``agent/prompts.py``), so the advertised path no longer
    resolved. The mismatch is unconditional (independent of round); the
    ``agent`` basename is just this layout's folder name, not a special case.

    ``runner.py`` sits INSIDE the snapshot but OUTSIDE the declared subtree:
    the non-mutable consumer whose reachability is the reason the snapshot
    root is a readable root at all. It is what distinguishes "walk the
    outermost roots" from "walk the declared subtrees" — both visit each file
    once, only the former still lets the proposer see who reads the value it
    is about to rewrite.
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
    mp = make_mutation_point(
        id="agent__system_prompt",
        file=agent / "prompts.py",
        # The adapter declared ``agent`` as the mutable subtree, so the
        # enumerated point's source_root IS that subtree under the snapshot.
        source_root=agent,
        content="You are a helpful assistant.",
    )
    return snapshot, (mp,)


def test_mutable_roots_admits_both_snapshot_and_subtree_relative_paths(tmp_path: Path) -> None:
    """A generation with a DECLARED mutable subtree keeps the snapshot root in
    its mutable surface, so the snapshot-relative path the manifest advertises
    (``agent/prompts.py``) resolves — alongside the bare subtree-relative
    ``prompts.py`` — regardless of which form the proposer issues
    (issue #20, acceptance #2)."""
    snapshot, mutations = _build_declared_subtree_snapshot(tmp_path)
    ctx = ProposerToolContext(
        workspace_root=tmp_path / "ws",
        generation_root=snapshot,
        epoch_id="ep-001",
        mutations=mutations,
    )
    roots = ctx.mutable_roots()
    # The snapshot root itself is in the surface (so a snapshot-relative path
    # resolves) AND the declared subtree (so a subtree-relative path resolves).
    assert snapshot.resolve() in roots
    assert (snapshot / "agent").resolve() in roots
    with bind_proposer_tool_context(ctx):
        # The exact call shape the default proposer issues — relative to the
        # snapshot root, i.e. carrying the ``agent/`` subtree prefix.
        text = read_mutable_file("agent/prompts.py")
        assert "You are a helpful assistant." in text
        # The subtree-relative form still resolves too.
        assert "You are a helpful assistant." in read_mutable_file("prompts.py")


def test_read_mutable_file_rejects_traversal(tmp_path: Path) -> None:
    # Plant a secret OUTSIDE the mutable subtree the proposer may read.
    secret = tmp_path / "snapshot" / "secret.txt"
    ctx = _make_ctx(tmp_path)
    secret.write_text("TOP SECRET", encoding="utf-8")
    with bind_proposer_tool_context(ctx):
        with pytest.raises(ValueError, match="does not resolve to a file"):
            read_mutable_file("../secret.txt")


def test_read_mutable_file_rejects_absolute(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    with bind_proposer_tool_context(ctx):
        with pytest.raises(ValueError, match="must be relative"):
            read_mutable_file("/etc/passwd")


def test_read_mutable_file_never_writes(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    target = tmp_path / "snapshot" / "harness" / "prompts.py"
    before = target.read_bytes()
    mtime_before = target.stat().st_mtime_ns
    with bind_proposer_tool_context(ctx):
        read_mutable_file("prompts.py")
    assert target.read_bytes() == before
    assert target.stat().st_mtime_ns == mtime_before


def test_grep_mutable_finds_matches(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    with bind_proposer_tool_context(ctx):
        out = grep_mutable(r"TODO")
    assert "router.py:" in out
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
    harness = tmp_path / "snapshot" / "harness"
    snapshot_before = {p: p.read_bytes() for p in harness.rglob("*") if p.is_file()}
    with bind_proposer_tool_context(ctx):
        grep_mutable(r".")
    for path, data in snapshot_before.items():
        assert path.read_bytes() == data


def test_grep_mutable_visits_each_file_once_under_a_declared_subtree(
    tmp_path: Path,
) -> None:
    """A declared mutable subtree must not be walked twice.

    ``mutable_roots`` returns the snapshot root AND each declared subtree so
    both path shapes RESOLVE, but the subtree is a descendant of the snapshot
    root: walking the list directly visited every file inside it once per
    containing root, emitting the same line under two different relative
    paths (``agent/prompts.py:1:`` and ``prompts.py:1:``) and burning the
    match budget on duplicates. Each match must appear exactly once, under
    the snapshot-relative path ``list_mutation_points`` advertises.

    Deduping must keep the OUTERMOST root, not the innermost: the exact
    result pins ``runner.py`` — inside the snapshot, outside the declared
    subtree — so a "walk only the declared subtrees" dedupe, which also
    visits each file once, still fails here for losing the non-mutable
    consumer the proposer needs in order to ground a rewrite.
    """
    snapshot, mutations = _build_declared_subtree_snapshot(tmp_path)
    ctx = ProposerToolContext(
        workspace_root=tmp_path / "ws",
        generation_root=snapshot,
        epoch_id="ep-001",
        mutations=mutations,
    )
    # Precondition: this layout really does surface both roots.
    assert snapshot.resolve() in ctx.mutable_roots()
    assert (snapshot / "agent").resolve() in ctx.mutable_roots()

    with bind_proposer_tool_context(ctx):
        out = grep_mutable(r"SYSTEM_PROMPT")

    lines = [line for line in out.splitlines() if line.strip()]
    assert lines == [
        "agent/prompts.py:1: SYSTEM_PROMPT = 'You are a helpful assistant.'",
        "runner.py:1: from agent.prompts import SYSTEM_PROMPT",
    ]


def test_grep_mutable_budget_is_not_spent_on_duplicates(tmp_path: Path) -> None:
    """The match cap counts distinct lines, not per-root revisits.

    With the snapshot root and the declared subtree both walked, a file with
    N matching lines produced 2N entries and truncated at half the real
    reach. Under a cap above the file's true match count but below the
    duplicated count, the result must now be complete and unannotated.
    """
    snapshot, mutations = _build_declared_subtree_snapshot(tmp_path)
    agent = snapshot / "agent"
    (agent / "prompts.py").write_text("HIT = 1\nHIT = 2\nHIT = 3\nHIT = 4\n", encoding="utf-8")
    ctx = ProposerToolContext(
        workspace_root=tmp_path / "ws",
        generation_root=snapshot,
        epoch_id="ep-001",
        mutations=mutations,
    )
    with bind_proposer_tool_context(ctx):
        with pytest.MonkeyPatch.context() as mp:
            # 4 real matches, 8 with the duplicate walk: a cap of 5 is
            # comfortably above the truth and below the duplicated count.
            mp.setattr(proposer_tools, "_GREP_MATCH_LIMIT", 5)
            out = grep_mutable(r"HIT")

    assert "truncated" not in out
    assert len([line for line in out.splitlines() if line.strip()]) == 4


def test_read_journal_and_insights_empty_when_absent(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    with bind_proposer_tool_context(ctx):
        assert read_journal() == ""
        assert read_insights() == ""


def test_read_journal_reads_existing(tmp_path: Path) -> None:
    from zicato.core.workspace import journal_path

    ctx = _make_ctx(tmp_path)
    jp = journal_path(ctx.workspace_root, ctx.epoch_id)
    jp.parent.mkdir(parents=True, exist_ok=True)
    jp.write_text("## round 1\nwe tried tightening the prompt.\n", encoding="utf-8")
    with bind_proposer_tool_context(ctx):
        out = read_journal()
    assert "tightening the prompt" in out


def test_tools_raise_with_no_bound_context() -> None:
    # Each tool resolves the context var and must raise cleanly when unbound.
    for tool in (list_mutation_points, read_journal, read_insights):
        with pytest.raises(RuntimeError, match="no bound ProposerToolContext"):
            tool()
    with pytest.raises(RuntimeError, match="no bound ProposerToolContext"):
        read_mutable_file("prompts.py")
    with pytest.raises(RuntimeError, match="no bound ProposerToolContext"):
        grep_mutable(r".")


def test_bind_context_sets_and_resets(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    # Unset before.
    assert proposer_tools._TOOL_CONTEXT.get() is None
    with bind_proposer_tool_context(ctx):
        assert proposer_tools._TOOL_CONTEXT.get() is ctx
    # Reset after the block, even though we entered cleanly.
    assert proposer_tools._TOOL_CONTEXT.get() is None


def test_bind_context_resets_on_exception(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    with pytest.raises(RuntimeError, match="boom"):
        with bind_proposer_tool_context(ctx):
            raise RuntimeError("boom")
    assert proposer_tools._TOOL_CONTEXT.get() is None


def test_default_proposer_tools_are_the_read_only_set() -> None:
    assert DEFAULT_PROPOSER_TOOLS == (
        list_mutation_points,
        read_mutable_file,
        grep_mutable,
        read_journal,
        read_insights,
        mutation_track_record,
        read_parent_diff,
        mutation_usage,
    )
