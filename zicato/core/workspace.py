"""Path math and construction-time invariants for the ``.zicato/`` workspace.

Pure path-resolution helpers — no I/O, no directory creation, no file
reads. Callers compose these helpers and then perform whatever I/O they
need; that separation keeps the path layout testable without a tmpdir
and lets the CLI introspect paths (e.g. ``zicato paths``) without
touching the filesystem.

The canonical layout these helpers produce::

    {workspace_root}/
      epochs/{epoch_id}/
        board.jsonl
        rubric.md
        scoring.json
        generations/{generation_id}/
          experiment.json
          runs/{entry_id}/
            events.jsonl
            loss.json
        journal.md
        analysis.md
      lineage.json

The two-callable invariant from :class:`zicato.core.types.RuntimeConfig`
is enforced here by :func:`assert_distinct_callables`; the dataclass
itself stays purely declarative.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any


def _epoch_root(workspace_root: Path, epoch_id: str) -> Path:
    return workspace_root / "epochs" / epoch_id


def _generation_root(
    workspace_root: Path, epoch_id: str, generation_id: str
) -> Path:
    return _epoch_root(workspace_root, epoch_id) / "generations" / generation_id


def epoch_dir(workspace_root: Path, epoch_id: str) -> Path:
    """Return the directory holding one epoch's artifacts."""
    return _epoch_root(workspace_root, epoch_id)


def generation_dir(
    workspace_root: Path, epoch_id: str, generation_id: str
) -> Path:
    """Return the directory holding one generation's artifacts."""
    return _generation_root(workspace_root, epoch_id, generation_id)


def run_dir(
    workspace_root: Path,
    epoch_id: str,
    generation_id: str,
    entry_id: str,
) -> Path:
    """Return the directory holding one run's artifacts.

    A run is one ``(epoch, generation, board_entry)`` triple; its
    directory holds the events JSONL and the reducer's loss profile.
    """
    return (
        _generation_root(workspace_root, epoch_id, generation_id)
        / "runs"
        / entry_id
    )


def events_jsonl_path(
    workspace_root: Path,
    epoch_id: str,
    generation_id: str,
    entry_id: str,
) -> Path:
    """Path to the goldfive event JSONL for one run."""
    return run_dir(workspace_root, epoch_id, generation_id, entry_id) / "events.jsonl"


def loss_profile_path(
    workspace_root: Path,
    epoch_id: str,
    generation_id: str,
    entry_id: str,
) -> Path:
    """Path to the reducer's ``loss.json`` output for one run."""
    return run_dir(workspace_root, epoch_id, generation_id, entry_id) / "loss.json"


def experiment_json_path(
    workspace_root: Path, epoch_id: str, generation_id: str
) -> Path:
    """Path to a generation's ``experiment.json`` (hypothesis + outcome)."""
    return _generation_root(workspace_root, epoch_id, generation_id) / "experiment.json"


def patches_dir(
    workspace_root: Path, epoch_id: str, generation_id: str
) -> Path:
    """Path to the per-patch JSON directory under a generation.

    See :doc:`project_zicato_storage_design` for the per-patch file
    layout. The directory is created lazily by writers; readers tolerate
    its absence (an experiment with zero patches has no directory).
    """
    return _generation_root(workspace_root, epoch_id, generation_id) / "patches"


def patch_json_path(
    workspace_root: Path,
    epoch_id: str,
    generation_id: str,
    patch_id: str,
) -> Path:
    """Path to one patch JSON file inside a generation's patches directory."""
    return patches_dir(workspace_root, epoch_id, generation_id) / f"{patch_id}.json"


def journal_path(workspace_root: Path, epoch_id: str) -> Path:
    """Path to an epoch's running narrative journal."""
    return _epoch_root(workspace_root, epoch_id) / "journal.md"


def analysis_path(workspace_root: Path, epoch_id: str) -> Path:
    """Path to an epoch's at-close analysis writeup."""
    return _epoch_root(workspace_root, epoch_id) / "analysis.md"


def lineage_path(workspace_root: Path) -> Path:
    """Path to the workspace-level cross-cutting lineage DAG."""
    return workspace_root / "lineage.json"


def rubric_path(workspace_root: Path, epoch_id: str) -> Path:
    """Path to the operator-edited proposer rubric for one epoch."""
    return _epoch_root(workspace_root, epoch_id) / "rubric.md"


def board_path(workspace_root: Path, epoch_id: str) -> Path:
    """Path to one epoch's frozen board JSONL."""
    return _epoch_root(workspace_root, epoch_id) / "board.jsonl"


def scoring_path(workspace_root: Path, epoch_id: str) -> Path:
    """Path to one epoch's frozen scoring-weights JSON."""
    return _epoch_root(workspace_root, epoch_id) / "scoring.json"


def assert_distinct_callables(
    harness_call_llm: Callable[..., Any],
    auxiliary_call_llm: Callable[..., Any],
) -> None:
    """Enforce that the two LLM callables on :class:`RuntimeConfig` differ.

    The emulator, proposer, judge, and analysis pass run on the
    ``auxiliary_call_llm`` side; the inner harness runs on the
    ``harness_call_llm`` side. If the two sides share a callable, the
    emulator and the inner harness execute through the same process
    state and risk colluding (the inner harness can perceive the
    emulator's prompts, the emulator can leak the expected output
    through shared state, etc.). The collusion risk is high enough that
    we refuse to start the run when the two callables are identity-
    equal.

    Raises
    ------
    RuntimeError
        If ``harness_call_llm is auxiliary_call_llm``.

    Notes
    -----
    Identity comparison (``is``) is intentional. Two distinct callables
    that happen to wrap the same underlying client / endpoint pass this
    check; that is the operator's responsibility. The point here is to
    catch the trivial mistake of passing the same callable twice.
    """
    if harness_call_llm is auxiliary_call_llm:
        raise RuntimeError(
            "harness_call_llm and auxiliary_call_llm must be distinct callables; "
            "shared callables risk collusion in multi-turn emulated entries"
        )


__all__ = [
    "epoch_dir",
    "generation_dir",
    "run_dir",
    "events_jsonl_path",
    "loss_profile_path",
    "experiment_json_path",
    "patches_dir",
    "patch_json_path",
    "journal_path",
    "analysis_path",
    "lineage_path",
    "rubric_path",
    "board_path",
    "scoring_path",
    "assert_distinct_callables",
]
