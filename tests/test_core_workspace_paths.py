"""Path-resolution tests for :mod:`zicato.core.workspace`.

The helpers under test are pure path math with a single I/O exception:
when ``workspace_root`` does not carry an ``epochs/`` directory but
``workspace_root / ".zicato" / "epochs"`` does, the helpers transparently
descend into the inner ``.zicato/`` dir. That descent makes the helpers
robust against the convention drift where some callers pass the outer
project dir and others pass the inner ``.zicato/`` dir.

These tests pin both invocation forms:

* The legacy form — ``workspace_root`` is itself the ``.zicato/`` dir
  (no descent triggered).
* The outer form — ``workspace_root`` is the project dir holding
  ``.zicato/`` (descent triggers).

And the guardrails:

* The descent is skipped when ``workspace_root / "epochs"`` already
  exists, so callers that built a synthetic ``{tmp}/epochs/`` layout
  aren't accidentally redirected.
* The descent is skipped when neither form has an ``epochs/`` dir, so
  the returned path is still useful for *writers* that create the tree
  lazily (the previous behaviour).
"""

from __future__ import annotations

from pathlib import Path

from zicato.core.workspace import (
    analysis_path,
    board_path,
    brief_path,
    epoch_dir,
    events_jsonl_path,
    experiment_json_path,
    field_tournament_path,
    field_tournaments_dir,
    generation_dir,
    journal_path,
    ladder_state_path,
    lineage_path,
    loss_profile_path,
    mutations_json_path,
    patch_json_path,
    patches_dir,
    rubric_path,
    run_dir,
    scoring_path,
)
from zicato.workspace import WorkspaceLayout


def test_epoch_dir_inner_form(tmp_path: Path) -> None:
    """Passing ``.zicato/`` directly resolves to ``.zicato/epochs/{id}``."""
    inner = tmp_path / ".zicato"
    (inner / "epochs" / "e0").mkdir(parents=True)
    assert epoch_dir(inner, "e0") == inner / "epochs" / "e0"


def test_epoch_dir_outer_form_descends(tmp_path: Path) -> None:
    """Passing the outer dir resolves into the inner ``.zicato/``."""
    inner = tmp_path / ".zicato"
    (inner / "epochs" / "e0").mkdir(parents=True)
    # Outer form: workspace_root points at ``tmp_path``, not ``.zicato``.
    # The helper should descend.
    assert epoch_dir(tmp_path, "e0") == inner / "epochs" / "e0"


def test_epoch_dir_legacy_outer_layout_unchanged(tmp_path: Path) -> None:
    """A workspace already laid out at ``{ws}/epochs/`` is left alone.

    Some tests + a few legacy workspaces materialise the epoch tree
    directly under the path they passed (no ``.zicato/`` wrapper). The
    descent must NOT trigger in that shape — the helper returns the
    path the caller built.
    """
    (tmp_path / "epochs" / "e0").mkdir(parents=True)
    assert epoch_dir(tmp_path, "e0") == tmp_path / "epochs" / "e0"


def test_epoch_dir_missing_both_returns_outer(tmp_path: Path) -> None:
    """When neither form exists, the helper returns the as-passed path.

    Writers create the tree lazily; the helper must still produce a
    useful target so the writer can ``mkdir(parents=True)`` against it.
    """
    out = epoch_dir(tmp_path, "e0")
    assert out == tmp_path / "epochs" / "e0"


def test_all_epoch_helpers_descend_uniformly(tmp_path: Path) -> None:
    """Every epoch-rooted helper picks up the descent identically.

    The fix is at the ``_epoch_root`` level so a regression in one
    helper would mean a regression in all of them; pin the family.
    """
    inner = tmp_path / ".zicato"
    (inner / "epochs" / "e0").mkdir(parents=True)

    edir = inner / "epochs" / "e0"
    assert epoch_dir(tmp_path, "e0") == edir
    assert generation_dir(tmp_path, "e0", "v0") == edir / "generations" / "v0"
    assert board_path(tmp_path, "e0") == edir / "board.jsonl"
    assert scoring_path(tmp_path, "e0") == edir / "scoring.json"
    assert journal_path(tmp_path, "e0") == edir / "journal.md"
    assert analysis_path(tmp_path, "e0") == edir / "analysis.md"
    assert mutations_json_path(tmp_path, "e0") == edir / "mutations.json"
    assert brief_path(tmp_path, "e0") == edir / "brief.md"
    # legacy alias resolves to the current brief.md path
    assert rubric_path(tmp_path, "e0") == edir / "brief.md"


def test_lineage_path_descends(tmp_path: Path) -> None:
    """``lineage_path`` (workspace-level) also handles the outer form."""
    inner = tmp_path / ".zicato"
    (inner / "epochs").mkdir(parents=True)
    assert lineage_path(tmp_path) == inner / "lineage.json"
    assert lineage_path(inner) == inner / "lineage.json"


def test_core_helpers_agree_with_workspace_layout(tmp_path: Path) -> None:
    """The ``core.workspace`` write-path helpers route through ``WorkspaceLayout``.

    The descent-normalised root and the layout share ONE definition of the
    leaf filename joins (read AND write). Pin that every ``core.workspace``
    helper produces exactly the path the layout resolves for the same
    coordinate — so a future divergence in either authority is caught here.
    The inner form (no descent) is used so the comparison is a pure path
    identity, not a descent test (that family is pinned above).
    """
    inner = tmp_path / ".zicato"
    layout = WorkspaceLayout.from_root(inner)

    assert epoch_dir(inner, "e0") == layout.epoch_dir("e0")
    assert generation_dir(inner, "e0", "v1") == layout.generation_dir("e0", "v1")
    assert run_dir(inner, "e0", "v1", "t1") == layout.run_dir("e0", "v1", "t1")
    assert events_jsonl_path(inner, "e0", "v1", "t1") == layout.events("e0", "v1", "t1")
    assert loss_profile_path(inner, "e0", "v1", "t1") == layout.loss("e0", "v1", "t1")
    assert experiment_json_path(inner, "e0", "v1") == layout.experiment("e0", "v1")
    assert patches_dir(inner, "e0", "v1") == layout.patches_dir("e0", "v1")
    assert patch_json_path(inner, "e0", "v1", "p3") == layout.patch_json("e0", "v1", "p3")
    assert mutations_json_path(inner, "e0") == layout.mutations("e0")
    assert ladder_state_path(inner, "e0") == layout.ladder_state("e0")
    assert journal_path(inner, "e0") == layout.journal("e0")
    assert analysis_path(inner, "e0") == layout.analysis_md("e0")
    assert lineage_path(inner) == layout.lineage_path
    assert brief_path(inner, "e0") == layout.brief("e0")
    assert board_path(inner, "e0") == layout.board("e0")
    assert scoring_path(inner, "e0") == layout.scoring("e0")
    assert field_tournaments_dir(inner, "e0") == layout.field_tournaments_dir("e0")
    assert field_tournament_path(inner, "e0", "v2") == layout.field_tournament("e0", "v2")


def test_workspace_layout_write_markers(tmp_path: Path) -> None:
    """The write-path marker methods added to ``WorkspaceLayout`` resolve correctly.

    ``current_generation`` (promoted-head marker), ``v0_seed_from`` (the
    cross-epoch roll seed marker), ``ladder_state.json``, the per-patch JSON
    file, and one round's field-tournament snapshot — the write-path leaf
    joins that previously lived as inline string joins in the orchestrator
    and epoching modules.
    """
    root = tmp_path / ".zicato"
    layout = WorkspaceLayout.from_root(root)
    edir = root / "epochs" / "e0"
    assert layout.current_generation_marker("e0") == edir / "current_generation"
    assert layout.roll_seed_marker("e0") == edir / "v0_seed_from"
    assert layout.ladder_state("e0") == edir / "ladder_state.json"
    assert layout.patch_json("e0", "v1", "p3") == (
        edir / "generations" / "v1" / "patches" / "p3.json"
    )
    assert layout.field_tournament("e0", "v2") == edir / "tournaments" / "field-v2.json"
