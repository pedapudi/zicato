"""Tests for ``zicato.synthetic.manifest_bridge``.

The bridge is the additive, goldfive-shaped second enumeration pass: a
root carrying an ``optimization/manifest.toml`` contributes one
:class:`MutationPoint` per manifest entry, so goldfive's declared prompt +
threshold surface is mutable without zicato markers being sprinkled
through an upstream tree.

It had no direct test coverage until the mutation surface was widened past
``*.py``, which put it squarely in the blast radius: the text pass now
walks the very ``.md`` and ``.toml`` files the bridge reads. The last
section here pins that the two passes compose without duplicating or
colliding.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from zicato.core.types import Patch
from zicato.mutation.enumerator import enumerate_mutations
from zicato.mutation.validator import validate_patches
from zicato.synthetic.manifest_bridge import enumerate_manifest_points, find_manifest


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body).lstrip("\n"), encoding="utf-8")


def _goldfive_tree(root: Path, *, manifest: str | None = None) -> Path:
    """Build a minimal goldfive-shaped worktree under ``root``.

    ``root`` is the checkout — the bridge's effective source root, and
    what every manifest ``source`` is relative to. Returns the
    ``goldfive/`` package directory inside it.
    """

    gf = root / "goldfive"
    _write(
        gf / "optimization" / "prompts" / "refine.md",
        """
        name: refine
        version: 3
        ---
        Refine the {plan_summary} given {drift_kind}.
        Be concise.
        """,
    )
    _write(gf / "steering.py", "REFINE_THRESHOLD = 0.42\n")
    _write(
        gf / "optimization" / "manifest.toml",
        manifest
        if manifest is not None
        else """
        [[mutation]]
        id = "refine_prompt"
        kind = "prompt"
        source = "goldfive/optimization/prompts/refine.md"
        python_attr = "REFINE_PROMPT"
        required_placeholders = ["{plan_summary}", "{drift_kind}"]
        tags = ["planner", "refine"]
        description = "The refine-step prompt body."

        [[mutation]]
        id = "refine_threshold"
        kind = "numeric"
        source = "goldfive/steering.py:REFINE_THRESHOLD"
        python_attr = "REFINE_THRESHOLD"
        type = "float"
        default = 0.42
        range = [0.0, 1.0]
        tags = ["planner"]
        description = "Drift score above which refine fires."
        """,
    )
    return gf


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


def test_no_manifest_yields_nothing(tmp_path: Path) -> None:
    _write(tmp_path / "mod.py", "X = 1\n")
    assert find_manifest(tmp_path) is None
    assert enumerate_manifest_points([tmp_path]) == []


def test_finds_manifest_under_the_goldfive_prefix(tmp_path: Path) -> None:
    gf = _goldfive_tree(tmp_path)
    assert find_manifest(tmp_path) == gf / "optimization" / "manifest.toml"


def test_finds_manifest_at_the_bare_optimization_path(tmp_path: Path) -> None:
    _write(tmp_path / "optimization" / "manifest.toml", "")
    assert find_manifest(tmp_path) == tmp_path / "optimization" / "manifest.toml"


def test_bare_optimization_layout_resolves_its_sources(tmp_path: Path) -> None:
    """The layout the old fixed ``parents[2]`` hop silently zeroed out.

    With the manifest at ``<root>/optimization/manifest.toml`` the caller
    used to resolve the effective root one level above ``<root>``, so
    every ``source`` pointed at a path that did not exist and the bridge
    returned nothing. Sources now resolve against ``<root>`` itself.
    """

    _write(tmp_path / "prompts" / "refine.md", "name: refine\n---\nBody.\n")
    _write(
        tmp_path / "optimization" / "manifest.toml",
        """
        [[mutation]]
        id = "bare_prompt"
        kind = "prompt"
        source = "prompts/refine.md"
        """,
    )
    (point,) = enumerate_manifest_points([tmp_path])
    assert point.id == "bare_prompt"
    assert point.source_root == tmp_path
    assert point.content == "Body."


def test_deep_probe_finds_a_snapshot_into_named_subdir_layout(tmp_path: Path) -> None:
    """The ``v0/snapshot/<basename>/`` shape the baseline seeder produces."""

    snapshot = tmp_path / "snapshot"
    gf = _goldfive_tree(snapshot / "goldfive_checkout")
    assert find_manifest(snapshot) == gf / "optimization" / "manifest.toml"
    ids = {p.id for p in enumerate_manifest_points([snapshot])}
    assert ids == {"refine_prompt", "refine_threshold"}


# --------------------------------------------------------------------------
# Point construction
# --------------------------------------------------------------------------


def test_prompt_entry_binds_the_body_after_the_separator(tmp_path: Path) -> None:
    gf = _goldfive_tree(tmp_path)
    points = {p.id: p for p in enumerate_manifest_points([tmp_path])}
    prompt = points["refine_prompt"]
    assert prompt.kind == "span"
    assert prompt.file == gf / "optimization" / "prompts" / "refine.md"
    assert prompt.source_root == tmp_path
    # The header metadata is NOT part of the mutable body.
    assert prompt.content == "Refine the {plan_summary} given {drift_kind}.\nBe concise."
    assert (prompt.line_start, prompt.line_end) == (4, 5)
    assert prompt.metadata["manifest_kind"] == "prompt"
    assert prompt.metadata["language"] == "markdown"
    assert prompt.metadata["python_attr"] == "REFINE_PROMPT"
    assert prompt.metadata["required_placeholders"] == "{plan_summary},{drift_kind}"
    assert prompt.metadata["tags"] == "planner,refine"
    assert prompt.metadata["description"] == "The refine-step prompt body."


def test_prompt_without_a_separator_takes_the_whole_file(tmp_path: Path) -> None:
    gf = _goldfive_tree(tmp_path)
    _write(gf / "optimization" / "prompts" / "refine.md", "Just a body.\n")
    (prompt,) = (p for p in enumerate_manifest_points([tmp_path]) if p.id == "refine_prompt")
    assert prompt.content == "Just a body."
    assert (prompt.line_start, prompt.line_end) == (1, 1)


def test_numeric_entry_carries_range_metadata(tmp_path: Path) -> None:
    gf = _goldfive_tree(tmp_path)
    points = {p.id: p for p in enumerate_manifest_points([tmp_path])}
    numeric = points["refine_threshold"]
    assert numeric.kind == "span"
    assert numeric.file == gf / "steering.py"
    assert numeric.content == "0.42"
    assert numeric.metadata["manifest_kind"] == "numeric"
    assert numeric.metadata["type"] == "float"
    assert numeric.metadata["min"] == "0.0"
    assert numeric.metadata["max"] == "1.0"


def test_points_are_sorted_deterministically(tmp_path: Path) -> None:
    _goldfive_tree(tmp_path)
    ids = [p.id for p in enumerate_manifest_points([tmp_path])]
    assert ids == sorted(ids)
    assert ids == [p.id for p in enumerate_manifest_points([tmp_path])]


# --------------------------------------------------------------------------
# Tolerance: the bridge is best-effort and must never crash the walk
# --------------------------------------------------------------------------


def test_malformed_manifest_toml_yields_nothing(tmp_path: Path) -> None:
    _goldfive_tree(tmp_path, manifest="this is = = not toml\n")
    assert enumerate_manifest_points([tmp_path]) == []


def test_non_array_mutation_key_yields_nothing(tmp_path: Path) -> None:
    _goldfive_tree(tmp_path, manifest='mutation = "not an array"\n')
    assert enumerate_manifest_points([tmp_path]) == []


def test_entry_with_a_missing_source_file_is_skipped(tmp_path: Path) -> None:
    _goldfive_tree(
        tmp_path,
        manifest="""
        [[mutation]]
        id = "gone"
        kind = "prompt"
        source = "goldfive/optimization/prompts/does_not_exist.md"
        """,
    )
    assert enumerate_manifest_points([tmp_path]) == []


def test_entry_with_unknown_kind_is_skipped(tmp_path: Path) -> None:
    _goldfive_tree(
        tmp_path,
        manifest="""
        [[mutation]]
        id = "weird"
        kind = "sculpture"
        source = "goldfive/optimization/prompts/refine.md"
        """,
    )
    assert enumerate_manifest_points([tmp_path]) == []


def test_entry_with_non_string_id_is_skipped(tmp_path: Path) -> None:
    _goldfive_tree(
        tmp_path,
        manifest="""
        [[mutation]]
        id = 7
        kind = "prompt"
        source = "goldfive/optimization/prompts/refine.md"
        """,
    )
    assert enumerate_manifest_points([tmp_path]) == []


def test_numeric_entry_without_a_colon_in_source_is_skipped(tmp_path: Path) -> None:
    _goldfive_tree(
        tmp_path,
        manifest="""
        [[mutation]]
        id = "bad_numeric"
        kind = "numeric"
        source = "goldfive/steering.py"
        """,
    )
    assert enumerate_manifest_points([tmp_path]) == []


# --------------------------------------------------------------------------
# Composition with the native passes
# --------------------------------------------------------------------------


def test_bridge_and_native_passes_compose_without_duplication(tmp_path: Path) -> None:
    """The regression the widened surface could have introduced.

    The text pass now walks ``.md`` and ``.toml``, which is exactly what
    the bridge reads. Since goldfive's prompt files and manifest carry no
    zicato markers, the text pass contributes nothing there and every
    manifest id appears exactly once.
    """

    gf = _goldfive_tree(tmp_path)
    _write(
        gf / "agent.py",
        '# zicato:mutable id="native_span"\nINSTRUCTION = "be helpful"\n',
    )
    points = enumerate_mutations([tmp_path])
    ids = [p.id for p in points]
    assert sorted(ids) == ["native_span", "refine_prompt", "refine_threshold"]
    assert len(ids) == len(set(ids))


def test_a_marked_prompt_file_is_the_only_way_the_two_passes_can_collide(
    tmp_path: Path,
) -> None:
    """Bridge ids and native ids CAN collide; the validator refuses.

    Nothing structurally prevents an operator from adding a zicato marker
    to a file the manifest already declares under the same id. The two
    passes are independent, so both points enumerate — and
    ``validate_patches`` rejects the ambiguous id rather than letting the
    applier edit whichever span happened to be enumerated last.
    """

    gf = _goldfive_tree(tmp_path)
    _write(
        gf / "optimization" / "prompts" / "refine.md",
        """
        name: refine
        ---
        <!-- zicato:mutable:code id="refine_prompt" -->
        Refine the {plan_summary} given {drift_kind}.
        <!-- zicato:mutable:end -->
        """,
    )
    points = enumerate_mutations([tmp_path])
    assert [p.id for p in points].count("refine_prompt") == 2

    patch = Patch(
        id="p",
        mutation_id="refine_prompt",
        op="replace",
        new_content="anything",
        new_numeric=None,
        new_enum=None,
        rationale="test",
    )
    problems = validate_patches([patch], enumeration=points)
    assert any("is ambiguous" in problem for problem in problems)


def test_unrelated_markers_in_a_goldfive_tree_still_enumerate(tmp_path: Path) -> None:
    """A goldfive worktree can carry native text surface of its own."""

    gf = _goldfive_tree(tmp_path)
    _write(
        gf / "optimization" / "prompts" / "extra.md",
        '<!-- zicato:mutable:file id="extra_prompt" -->\nbody\n',
    )
    ids = {p.id for p in enumerate_mutations([tmp_path])}
    assert ids == {"refine_prompt", "refine_threshold", "extra_prompt"}
