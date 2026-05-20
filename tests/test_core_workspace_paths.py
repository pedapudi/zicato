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
    epoch_dir,
    generation_dir,
    journal_path,
    lineage_path,
    mutations_json_path,
    rubric_path,
    scoring_path,
)


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
    assert rubric_path(tmp_path, "e0") == edir / "rubric.md"


def test_lineage_path_descends(tmp_path: Path) -> None:
    """``lineage_path`` (workspace-level) also handles the outer form."""
    inner = tmp_path / ".zicato"
    (inner / "epochs").mkdir(parents=True)
    assert lineage_path(tmp_path) == inner / "lineage.json"
    assert lineage_path(inner) == inner / "lineage.json"
