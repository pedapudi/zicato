"""Constructors for the runtime objects scripted tests build by hand.

A test that drives the tournament runner, a worker subprocess or a CLI
command needs a :class:`~zicato.core.RuntimeConfig` and, often, a
:class:`~zicato.core.Generation` and a seeded lineage — none of which is
the subject of the test. Before this module each such test carried its
own copy of the same constructor, and the copies drifted only in the
name they were given, never in what they built.

The builders here are deliberately minimal: they construct the smallest
object the runtime accepts, with values that carry no meaning beyond
being distinct. A test whose subject IS one of these values passes it
explicitly or builds the object itself.
"""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from zicato.board.jsonl import save_board
from zicato.core import BoardEntry, Generation, RuntimeConfig, ScoringWeights
from zicato.epoch.contract import ContractInputs
from zicato.epoch.lifecycle import new_epoch, scoring_to_dict
from zicato.epoch.lineage import append_to_lineage
from zicato.models_config import execution_roles_for_runtime
from zicato.runtime.lock import acquire_workspace_lock
from zicato.tournament.scoring import write_gen_score


async def empty_target_call(system: str, user: str, model: str) -> str:
    return ""


async def empty_evaluation_call(system: str, user: str, model: str) -> str:
    return ""


def prepare_tournament_epoch(
    workspace_root: Path,
    config: RuntimeConfig,
    board: list[BoardEntry],
    weights: ScoringWeights,
    *,
    name: str = "tournament",
) -> str:
    """Publish the supplied evaluation inputs and return their actual epoch id."""
    workspace_root.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".tournament-inputs-", dir=workspace_root) as temporary:
        source = Path(temporary)
        board_path, brief_path, scoring_path = (
            source / "board.jsonl",
            source / "brief.md",
            source / "scoring.json",
        )
        save_board(board, board_path)
        brief_path.write_text("# Goal\nMeasure the supplied deterministic candidate.\n")
        scoring_path.write_text(json.dumps(scoring_to_dict(weights)))
        inputs = ContractInputs(
            board_path=board_path,
            brief_path=brief_path,
            scoring_path=scoring_path,
            entrypoint="",
            mutable_trees=(),
            execution_roles=execution_roles_for_runtime(config),
        )
        return new_epoch(workspace_root, name, board_path, brief_path, weights, contract=inputs).id


def record_tournament_score(
    workspace_root: Path, epoch_id: str, generation_id: str, aggregate: dict[str, Any]
) -> None:
    """Publish a fixture's historical score through its canonical writer."""
    with acquire_workspace_lock(workspace_root, "fixture-score"):
        write_gen_score(workspace_root, epoch_id, generation_id, aggregate)


def seed_baseline(workspace: Path, epoch_id: str) -> Generation:
    """Publish the registered baseline snapshot and return its generation."""
    from zicato import workspace_loader
    from zicato.evolve.generation_phase import current_generation, snapshot_root
    from zicato.evolve.round_baseline import _ensure_baseline_snapshot

    workspace_config = workspace_loader.load_workspace_config(workspace)

    with acquire_workspace_lock(workspace, "contract-test") as writer:
        _ensure_baseline_snapshot(workspace, epoch_id, workspace_config, writer=writer)
    champion_id = current_generation(workspace, epoch_id)
    return Generation(
        id=champion_id,
        epoch_id=epoch_id,
        parent_id=None,
        snapshot_root=snapshot_root(workspace, epoch_id, champion_id),
        created_at="",
        promoted=True,
    )


def runtime_config(tmp_path: Path) -> RuntimeConfig:
    """A RuntimeConfig whose two LLM callables return the empty string.

    The harness and evaluation callables are separate function objects, not
    one function bound twice: the runner re-checks that the two callables
    are identity-unequal as defense in depth, so a config that reused a
    single callable would fail that check for a reason unrelated to the
    test's subject.
    """

    return RuntimeConfig(
        instance_id="test",
        workspace_root=tmp_path,
        target_call_llm=empty_target_call,
        evaluation_call_llm=empty_evaluation_call,
    )


def make_generation(workspace: Path, gen_id: str = "v0") -> Generation:
    """A parentless generation whose snapshot directory exists on disk.

    The snapshot root is created eagerly because the callers hand the
    generation to code that reads the directory.
    """
    snap = workspace / "snap" / gen_id
    snap.mkdir(parents=True, exist_ok=True)
    return Generation(
        id=gen_id,
        epoch_id="e0",
        parent_id=None,
        snapshot_root=snap,
        created_at="2026-05-15T00:00:00Z",
    )


def seed_promoted_lineage(ws: Path, epoch_id: str) -> None:
    """Register a promoted seed ``v0`` and a promoted child ``v1`` in lineage.

    The snapshot roots are paths that are never read — the callers exercise
    lineage and index bookkeeping, not snapshot contents.
    """
    g0 = Generation(
        id="v0",
        epoch_id=epoch_id,
        parent_id=None,
        snapshot_root=Path("/tmp/snap/v0"),
        created_at="2026-01-01T00:00:00Z",
        promoted=True,
    )
    g1 = Generation(
        id="v1",
        epoch_id=epoch_id,
        parent_id="v0",
        snapshot_root=Path("/tmp/snap/v1"),
        created_at="2026-01-02T00:00:00Z",
        promoted=True,
    )
    append_to_lineage(ws, epoch_id, g0, None)
    append_to_lineage(ws, epoch_id, g1, "v0")
