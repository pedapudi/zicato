"""Smoke tests for the vendored target_1_presentation example.

These tests are intentionally light: they verify the example's static
surface (import path, mutation markers, board parseability) without
exercising the full ADK runtime. End-to-end execution lives behind
the zicato runner and is covered by the runner's own test suite once
that branch lands.
"""

from __future__ import annotations

import importlib
import json
import re
from pathlib import Path

import pytest

# Resolve the example directory through the installed
# ``zicato_examples`` package so the test is independent of where the
# examples distribution lives on disk.
import zicato_examples.target_1_presentation as _t1_pkg  # noqa: E402

EXAMPLE_DIR = Path(_t1_pkg.__file__).resolve().parent
AGENT_DIR = EXAMPLE_DIR / "agent"
BOARD_PATH = EXAMPLE_DIR / "board.jsonl"
SCORING_PATH = EXAMPLE_DIR / "scoring.json"


# ---------------------------------------------------------------------------
# Agent module import + root_agent accessor
# ---------------------------------------------------------------------------


def test_agent_module_imports() -> None:
    """The vendored agent package imports cleanly without optional extras.

    ADK may or may not be installed in this branch's CI; the module is
    written so the bare import is side-effect free and ``build_agent_tree``
    is the only place that touches ``google.adk``.
    """
    mod = importlib.import_module("zicato_examples.target_1_presentation.agent")
    assert hasattr(mod, "build_agent_tree")
    # ``root_agent`` is a PEP 562 lazy attribute — we don't materialise it
    # in this test because doing so requires google-adk to be installed.
    # The static presence of the attribute on the submodule is asserted
    # below by reading the source.


def test_agent_module_exposes_root_agent_symbol() -> None:
    """``agent/agent.py`` declares ``root_agent`` in its public surface.

    We don't materialise the agent (that requires ADK and a model). We
    just verify the symbol is reachable via the package's public surface
    (``__getattr__`` lazy build) by reading ``__all__`` on the package
    init.
    """
    init_path = AGENT_DIR / "__init__.py"
    text = init_path.read_text()
    assert "root_agent" in text, (
        "agent/__init__.py must re-export root_agent so the harness "
        "adapter can resolve it via the package's public surface."
    )

    agent_path = AGENT_DIR / "agent.py"
    agent_text = agent_path.read_text()
    assert "def build_agent_tree(" in agent_text
    # Lazy ``root_agent`` is materialised through PEP 562 __getattr__.
    assert "__getattr__" in agent_text
    assert "root_agent" in agent_text


# ---------------------------------------------------------------------------
# Mutation-marker count
# ---------------------------------------------------------------------------


# Matches all three opening-marker variants (span / file / code) so the
# count walk sees the pointed instruction spans AND the slug/path code
# regions. The ``:end`` sentinel carries no id and is not matched.
_MARKER_RE = re.compile(r'#\s*zicato:mutable(?::file|:code)?\s+id="([a-zA-Z0-9_]+)"')


def _walk_python_files(root: Path) -> list[Path]:
    """Return every ``.py`` file under ``root`` (no ``__pycache__``)."""
    return [p for p in sorted(root.rglob("*.py")) if "__pycache__" not in p.parts]


def _marker_ids() -> set[str]:
    ids: set[str] = set()
    for py_file in _walk_python_files(AGENT_DIR):
        for line in py_file.read_text().splitlines():
            m = _MARKER_RE.search(line)
            if m is not None:
                ids.add(m.group(1))
    return ids


def test_mutation_markers_minimum_count() -> None:
    """The vendored agent declares an expanded mutation surface.

    After splitting the broad instruction spans into pointed sub-clauses
    and exposing the slug/path tool code as ``# zicato:mutable:code``
    regions, the surface grew well past the original 6-id floor. We pin a
    higher floor (12) as the regression guard — if a refactor strips the
    pointed routing/topic spans or the slug/path code regions, this test
    catches it.
    """
    ids = _marker_ids()
    assert len(ids) >= 12, (
        f"Expected at least 12 distinct zicato:mutable ids in {AGENT_DIR}, "
        f"got {len(ids)}: {sorted(ids)}"
    )


def test_mutation_ids_include_routing_and_specialist_instructions() -> None:
    """Spot-check that the coordinator + at least one specialist are annotated.

    Both the dogfood-target plan and the rubric call out the coordinator's
    routing instruction and the specialists' system instructions as the
    primary mutation surface. This is a belt-and-braces check on top of
    the count assertion above.
    """
    ids = _marker_ids()

    assert "coordinator_instruction" in ids
    # At least one specialist instruction must be present. We don't pin
    # the exact name to keep refactors cheap — any of these is sufficient.
    specialist_ids = {
        "researcher_instruction",
        "web_developer_instruction",
        "writer_instruction",
        "reviewer_instruction",
        "debugger_instruction",
    }
    assert ids & specialist_ids, (
        "At least one specialist instruction must carry a "
        "zicato:mutable marker. Found ids: " + repr(sorted(ids))
    )


def test_mutation_ids_include_pointed_routing_and_topic_spans() -> None:
    """The pointed instruction sub-clauses are individually addressable.

    The dominant board failure is a write/read slug mismatch driven by
    the coordinator's files_not_found routing and the developer's /
    reviewer's topic-naming. Each of those sub-decisions must be its own
    pointed mutation id so the proposer can rewrite it without disturbing
    the rest of the instruction.
    """
    ids = _marker_ids()
    for pointed in (
        "coordinator_files_not_found_routing",
        "web_developer_topic_naming",
        "reviewer_read_path",
    ):
        assert pointed in ids, f"missing pointed span id {pointed!r}; found {sorted(ids)}"


def test_slug_path_tool_code_is_mutable_code_surface() -> None:
    """The slugify / output-path tool logic enumerates as ``code`` points.

    This is the surface the proposer needs to actually fix the
    file-finding failure: rewriting WHERE files are written and HOW they
    are located. We enumerate through the real framework (not a regex)
    so the assertion also proves the code regions are well-formed
    (matched ``:code`` / ``:end`` pairs) and resolve to ``kind="code"``.
    """
    from zicato.mutation.enumerator import enumerate_mutations

    points = enumerate_mutations([AGENT_DIR])
    by_id = {p.id: p for p in points}

    code_ids = {"topic_slugify_logic", "topic_output_dir_logic", "find_presentation_match_logic"}
    for cid in code_ids:
        assert cid in by_id, f"missing code-region id {cid!r}; found {sorted(by_id)}"
        assert by_id[cid].kind == "code", f"{cid!r} should be kind=code, got {by_id[cid].kind!r}"

    # The slug normalization that determines the on-disk path is in the
    # mutable set and its current body is the live ``replace`` rule.
    assert "replace(" in by_id["topic_slugify_logic"].content

    # The pointed instruction sub-clauses enumerate as string spans, and
    # the tool docstrings remain span points too — the full surface is a
    # mix of span + code kinds.
    kinds = {p.kind for p in points}
    assert kinds == {"span", "code"}, kinds


def test_agent_module_enumerated_points_apply_and_reimport(tmp_path: Path) -> None:
    """An end-to-end check: a code-region patch applies to a fresh copy
    of the agent package and the copy still imports side-effect free."""
    import importlib.util

    from zicato.core.types import Patch
    from zicato.mutation.applier import apply_patches

    tgt = tmp_path / "agent_copy"
    patch = Patch(
        id="p1",
        mutation_id="topic_slugify_logic",
        op="replace",
        new_content='    slug = topic.strip().lower().replace(" ", "-").replace("/", "-")\n',
        new_numeric=None,
        new_enum=None,
        rationale="unify write/read slug",
    )
    apply_patches(AGENT_DIR, [patch], tgt)

    patched = (tgt / "agent.py").read_text()
    assert 'replace(" ", "-")' in patched

    # Import the patched module under a throwaway name and prove the
    # lazy ``root_agent`` pattern survives (no eager ADK construction on
    # import) and the rewritten slug logic is live.
    spec = importlib.util.spec_from_file_location("t1_agent_patched_under_test", tgt / "agent.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert hasattr(mod, "build_agent_tree")
    assert mod._slugify_topic("Hello World") == "hello-world"  # type: ignore[attr-defined]


def test_agent_realistic_misindented_slug_fix_applies_and_reimports(tmp_path: Path) -> None:
    """The proposer's realistic, MIS-INDENTED multi-line slug fix applies to
    a fresh copy of the real agent.py: the file still parses, retains its
    top-level imports, the ``:code`` id re-resolves, and the patched module
    re-imports with the new slug logic live.

    This is the end-to-end reproduction of BUG A — before the applier
    re-anchored ``:code`` bodies, a column-0 multi-line replacement of
    ``topic_slugify_logic`` produced an unparseable agent.py that dropped
    every top-level import and stopped re-enumerating.
    """
    import importlib.util

    from zicato.core.types import Patch
    from zicato.mutation.applier import apply_patches
    from zicato.mutation.enumerator import enumerate_mutations

    tgt = tmp_path / "agent_copy"
    # A realistic proposer body: strip + suffix-normalize, emitted at the
    # wrong (column-0) indent across multiple lines.
    new_content = (
        'slug = topic.strip().lower().replace(" ", "_").replace("/", "_")\n'
        'for suffix in ("_presentation", "_slideshow"):\n'
        "    if slug.endswith(suffix):\n"
        "        slug = slug[: -len(suffix)]\n"
    )
    patch = Patch(
        id="p1",
        mutation_id="topic_slugify_logic",
        op="replace",
        new_content=new_content,
        new_numeric=None,
        new_enum=None,
        rationale="strip suffix + unify slug",
    )
    apply_patches(AGENT_DIR, [patch], tgt)

    patched = (tgt / "agent.py").read_text()
    import ast as _ast

    _ast.parse(patched)  # still valid Python
    # Top-level imports survive.
    assert "from __future__ import annotations" in patched
    assert "import os" in patched
    assert "from typing import Any" in patched
    # The ``:code`` id still resolves after the apply.
    by_id = {p.id: p for p in enumerate_mutations([tgt])}
    assert by_id["topic_slugify_logic"].kind == "code"

    # Re-import the patched module and prove the new logic is live.
    spec = importlib.util.spec_from_file_location("t1_agent_misindent_under_test", tgt / "agent.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod._slugify_topic("Cool Topic Presentation") == "cool_topic"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# board.jsonl — lazy validation via zicato.board.jsonl.load_board
# ---------------------------------------------------------------------------


def test_board_jsonl_exists_and_is_well_formed() -> None:
    """Every line of ``board.jsonl`` is valid JSON.

    This test does NOT require ``zicato.board.jsonl`` to exist yet — it
    just checks the file is parseable line-by-line. The deeper
    discriminant-validation lives below behind a lazy import.
    """
    assert BOARD_PATH.is_file(), f"missing {BOARD_PATH}"
    line_count = 0
    with BOARD_PATH.open() as f:
        for i, line in enumerate(f, start=1):
            line = line.rstrip("\n")
            if not line.strip():
                continue
            try:
                json.loads(line)
            except json.JSONDecodeError as e:
                pytest.fail(f"board.jsonl line {i}: invalid JSON ({e})")
            line_count += 1
    assert line_count >= 6, f"Expected at least 6 board entries in {BOARD_PATH}, got {line_count}."


def test_board_jsonl_loads_via_zicato_board_loader() -> None:
    """When ``zicato.board.jsonl.load_board`` is importable, every entry validates.

    The board loader ships from a parallel branch (``r2/board``). When
    that branch lands and merges, this test exercises the full
    discriminant validation. Until then we skip — a board-loader gap is
    a fix-it-in-the-other-branch problem, not a regression of this
    example.
    """
    try:
        load_board = importlib.import_module("zicato.board.jsonl").load_board
    except (ImportError, AttributeError) as e:
        pytest.skip(
            f"zicato.board.jsonl.load_board not importable yet ({e}); deferred to integration."
        )

    try:
        entries = load_board(BOARD_PATH)
    except Exception as e:  # noqa: BLE001 — surface the validator's reason
        pytest.fail(f"load_board rejected board.jsonl: {e!r}")

    assert len(entries) >= 6
    ids = {getattr(e, "id", None) for e in entries}
    assert "waffles_single" in ids
    assert "picky_stakeholder_emulated" in ids


def test_board_jsonl_validates_via_validate_board_entry() -> None:
    """Each entry validates via ``zicato.core.types.validate_board_entry``.

    This is the underlying validator the board loader will call. It
    lives on the core types module which has shipped on main, so
    unlike ``load_board`` this test does not need to skip. The leading
    ``board_meta`` header line, when present, is board-level metadata
    rather than an entry and is skipped here.
    """
    from zicato.core.types import validate_board_entry

    with BOARD_PATH.open() as f:
        for i, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            if d.get("board_meta") is True:
                # Board-level metadata header — not a BoardEntry.
                continue
            try:
                validate_board_entry(d)
            except Exception as e:  # noqa: BLE001
                pytest.fail(
                    f"board.jsonl line {i} (id={d.get('id')!r}) failed "
                    f"discriminant validation: {e!r}"
                )


# ---------------------------------------------------------------------------
# scoring.json — round-trip through ScoringWeights
# ---------------------------------------------------------------------------


def test_scoring_json_loads_into_scoring_weights() -> None:
    """``scoring.json`` round-trips through the ScoringWeights dataclass.

    The proposer reads this file at epoch creation time; a malformed
    file would surface as a runtime error well after the operator
    typed ``zicato epoch new``. Catch it here at lint time instead.
    """
    from zicato.core.types import ScoringWeights

    with SCORING_PATH.open() as f:
        d = json.load(f)
    sw = ScoringWeights(**d)
    # Spot-check a couple of fields we know the rubric leans on.
    assert sw.namespace_weights["drift:"] == 1.0
    assert sw.pass_rate_monotonicity is True
    assert sw.severity_weights["critical"] >= sw.severity_weights["warning"]
