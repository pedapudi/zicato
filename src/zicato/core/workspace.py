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

Convention drift — outer vs inner workspace root
-------------------------------------------------

``workspace_root`` is, by convention, the inner ``.zicato`` directory
itself: ``epochs/``, ``runtime/``, etc. hang directly off it. Some
callers historically pass the *outer* project directory (the parent that
holds ``.zicato/``) — when that happens the helpers below
transparently descend into ``.zicato/`` when the inner layout exists.
This is the single I/O exception in this module: a best-effort
``Path.is_dir()`` probe that lets the report regenerator + dashboard
read the right tree even when the caller hands us the outer dir.

The descent only fires when the outer form does NOT carry an
``epochs/`` directory but the inner ``.zicato/`` does; legacy callers
that already pass the inner dir, and tests that build a synthetic
``{ws}/epochs/`` tree, are untouched.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any


def _normalise_workspace_root(workspace_root: Path) -> Path:
    """Resolve ``workspace_root`` to the inner ``.zicato/`` dir when needed.

    The path helpers treat ``workspace_root`` as the inner ``.zicato``
    directory; epoch artifacts live at
    ``workspace_root / "epochs" / {epoch_id}``. Some callers — historic
    or convenience wrappers — pass the *outer* project dir instead. To
    keep those callers working without surprise, this normaliser
    descends into ``workspace_root / ".zicato"`` when:

    * the outer form does NOT carry an ``epochs/`` directory, AND
    * the inner form DOES carry one.

    Otherwise the path is returned unchanged. The behaviour is
    deliberately conservative: legacy layouts where ``{ws}/epochs/``
    already exists are never overridden, and tests that build a
    synthetic ``{tmp}/epochs/`` tree don't accidentally redirect.
    """
    root = Path(workspace_root)
    if (root / "epochs").is_dir():
        return root
    inner = root / ".zicato"
    if (inner / "epochs").is_dir():
        return inner
    return root


def _epoch_root(workspace_root: Path, epoch_id: str) -> Path:
    return _normalise_workspace_root(workspace_root) / "epochs" / epoch_id


def _generation_root(workspace_root: Path, epoch_id: str, generation_id: str) -> Path:
    return _epoch_root(workspace_root, epoch_id) / "generations" / generation_id


def epoch_dir(workspace_root: Path, epoch_id: str) -> Path:
    """Return the directory holding one epoch's artifacts."""
    return _epoch_root(workspace_root, epoch_id)


def generation_dir(workspace_root: Path, epoch_id: str, generation_id: str) -> Path:
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
    return _generation_root(workspace_root, epoch_id, generation_id) / "runs" / entry_id


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


def experiment_json_path(workspace_root: Path, epoch_id: str, generation_id: str) -> Path:
    """Path to a generation's ``experiment.json`` (hypothesis + outcome)."""
    return _generation_root(workspace_root, epoch_id, generation_id) / "experiment.json"


def patches_dir(workspace_root: Path, epoch_id: str, generation_id: str) -> Path:
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


def mutations_json_path(workspace_root: Path, epoch_id: str) -> Path:
    """Path to an epoch's per-round mutation-points snapshot.

    Written by the orchestrator after each round's mutation enumeration:
    a JSON array of the :class:`zicato.core.types.MutationPoint` records
    the proposer was offered. The dashboard reads this to render the
    mutable surface for an in-progress epoch without re-walking the
    snapshot tree. The file lives under the epoch directory (not a
    generation directory) and is overwritten every round — it always
    reflects the most recent enumeration.
    """
    return _epoch_root(workspace_root, epoch_id) / "mutations.json"


def journal_path(workspace_root: Path, epoch_id: str) -> Path:
    """Path to an epoch's running narrative journal."""
    return _epoch_root(workspace_root, epoch_id) / "journal.md"


def analysis_path(workspace_root: Path, epoch_id: str) -> Path:
    """Path to an epoch's at-close analysis writeup."""
    return _epoch_root(workspace_root, epoch_id) / "analysis.md"


def lineage_path(workspace_root: Path) -> Path:
    """Path to the workspace-level cross-cutting lineage DAG."""
    return _normalise_workspace_root(workspace_root) / "lineage.json"


def rubric_path(workspace_root: Path, epoch_id: str) -> Path:
    """Path to the operator-edited proposer rubric for one epoch."""
    return _epoch_root(workspace_root, epoch_id) / "rubric.md"


def board_path(workspace_root: Path, epoch_id: str) -> Path:
    """Path to one epoch's frozen board JSONL."""
    return _epoch_root(workspace_root, epoch_id) / "board.jsonl"


def field_tournaments_dir(workspace_root: Path, epoch_id: str) -> Path:
    """Path to an epoch's durable field-tournament snapshot directory.

    A non-gauntlet structure (swiss / single-elim / double-elim / racing)
    settles ONE field record per round's tournament — the round-by-round
    pairings + the Copeland standings + the proposing field-status — which
    the per-challenger ``experiment.json`` audit cannot reconstruct on its
    own. The orchestrator writes that settled field structure here so the
    analytical index can re-derive it on ``zicato reindex`` (the
    files-are-canonical rule) and the dashboard renders the ladder /
    bracket post-run rather than blank.

    One file per field tournament: ``field-{first_challenger}.json``,
    keyed on the round's first applied challenger so a multi-round epoch
    keeps a snapshot per round without clobbering earlier ones. The
    directory is created lazily by the writer; readers tolerate its
    absence (a pure-gauntlet epoch never writes one).
    """
    return _epoch_root(workspace_root, epoch_id) / "tournaments"


def field_tournament_path(workspace_root: Path, epoch_id: str, first_challenger_id: str) -> Path:
    """Path to one round's durable field-tournament snapshot JSON.

    See :func:`field_tournaments_dir`. ``first_challenger_id`` is the
    round's first applied challenger — the same stable key the runtime
    ``active_tournament`` tournament id is minted from — so the snapshot
    is idempotent across rebuilds and unique per round.
    """
    return field_tournaments_dir(workspace_root, epoch_id) / f"field-{first_challenger_id}.json"


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
    "mutations_json_path",
    "journal_path",
    "analysis_path",
    "lineage_path",
    "rubric_path",
    "board_path",
    "scoring_path",
    "field_tournaments_dir",
    "field_tournament_path",
    "assert_distinct_callables",
]
