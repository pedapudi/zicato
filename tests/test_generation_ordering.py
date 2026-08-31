"""Every generation enumeration orders ``v2`` before ``v10``.

Generation ids are minted as ``v`` followed by the round number, so a
lexical sort of eleven or more generations puts ``v10`` between ``v1`` and
``v2`` and hands its caller a scrambled history. Each test builds an epoch
with eleven generations — the smallest count at which lexical and
round-number order differ — and pins the enumeration to round-number order.
The single ordering definition is :mod:`zicato.workspace.epochs`.
"""

from __future__ import annotations

import json
from pathlib import Path

from zicato.workspace import (
    WorkspaceLayout,
    generation_round_number,
    natural_key,
    next_generation_id,
)

#: v0 through v10: the smallest epoch whose lexical order is wrong.
ELEVEN = [f"v{n}" for n in range(11)]
LEXICAL = sorted(ELEVEN)


def _make_generations(workspace: Path, epoch_id: str, ids: list[str] = ELEVEN) -> Path:
    """Create the record directory of every id, in an order that is neither
    lexical nor numeric, so a test cannot pass on directory creation order."""
    gens_root = WorkspaceLayout.from_root(workspace).generations_dir(epoch_id)
    for gen_id in sorted(ids, reverse=True):
        (gens_root / gen_id).mkdir(parents=True)
    return gens_root


def test_ordering_primitives_agree_on_round_number_order() -> None:
    # The premise every test here defends: sorted() alone inverts v2/v10.
    assert LEXICAL.index("v10") < LEXICAL.index("v2")
    assert sorted(LEXICAL, key=natural_key) == ELEVEN
    # Descending, as the recombination pool walks it: newest generation first.
    assert sorted(LEXICAL, key=natural_key, reverse=True) == ELEVEN[::-1]
    assert generation_round_number("v10") == 10
    assert generation_round_number("named") is None


def test_epoch_analysis_collects_experiments_in_round_number_order(tmp_path: Path) -> None:
    """The at-epoch-close analysis pass reads experiment.json in lineage order."""
    from zicato.epoch.analysis import _collect_experiments

    workspace = tmp_path / ".zicato"
    gens_root = _make_generations(workspace, "e0")
    for gen_id in ELEVEN:
        (gens_root / gen_id / "experiment.json").write_text(
            json.dumps({"generation_id": gen_id}), encoding="utf-8"
        )

    assert [d["generation_id"] for d in _collect_experiments(workspace, "e0")] == ELEVEN


def test_dashboard_views_list_generations_in_round_number_order(tmp_path: Path) -> None:
    """The mutation browser's generation columns and the file-tree view's
    per-epoch generation list both read left to right as the epoch ran."""
    from zicato.dashboard.filetree import build_file_index
    from zicato.dashboard.mutations import _generation_ids, recorded_generation_ids
    from zicato.query import WorkspacePaths

    workspace = tmp_path / ".zicato"
    _make_generations(workspace, "e0")
    (workspace / "epochs" / "e0" / "config.json").write_text(
        json.dumps({"created_at": "2026-01-01T00:00:00Z"}), encoding="utf-8"
    )
    paths = WorkspacePaths(workspace)

    assert recorded_generation_ids(paths, "e0") == ELEVEN
    assert _generation_ids(None, paths, "e0") == (ELEVEN, False)
    epoch = next(row for row in build_file_index(paths)["epochs"] if row["epoch_id"] == "e0")
    assert [g["generation_id"] for g in epoch["generations"]] == ELEVEN


def test_health_command_lists_generations_in_round_number_order(tmp_path: Path) -> None:
    """``zicato health`` feeds its window-based detectors a lineage-ordered
    history."""
    from zicato.cli.commands.health import _generation_ids

    workspace = tmp_path / ".zicato"
    _make_generations(workspace, "e0")

    assert _generation_ids(workspace, "e0") == ELEVEN


def test_epoch_health_inputs_read_generations_in_round_number_order(tmp_path: Path) -> None:
    """The per-round loop-health assessment reads losses per generation in
    lineage order, so its trend windows see the epoch as it ran."""
    from zicato.core.types import BoardEntry
    from zicato.core.workspace import loss_profile_path
    from zicato.evolve.round_reporting import _collect_epoch_health_inputs

    workspace = tmp_path / ".zicato"
    _make_generations(workspace, "e0")
    for gen_id in ELEVEN:
        path = loss_profile_path(workspace, "e0", gen_id, "entry_a")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "run_id": f"r-{gen_id}",
                    "entry_id": "entry_a",
                    "generation_id": gen_id,
                    "epoch_id": "e0",
                    "drift_counts": [],
                    "plan_revisions": 0,
                    "task_failure_ratio": 0.0,
                    "runtime_ms": 100,
                    "wall_clock_budget_exceeded": False,
                    "expectation_result": None,
                    "drift_loss": 1.0,
                    "pass_fail": True,
                }
            ),
            encoding="utf-8",
        )

    board = [BoardEntry(id="entry_a", kind="single_turn", wall_clock_budget_seconds=60, input="hi")]
    losses_by_generation, _ = _collect_epoch_health_inputs(workspace, "e0", board)
    assert list(losses_by_generation) == ELEVEN


def test_current_and_latest_generation_are_the_highest_round(tmp_path: Path) -> None:
    """Both resolvers of "where is this epoch now" answer v10, not v9."""
    from zicato.evolve import generation_phase
    from zicato.runtime.resume import _latest_generation_id

    workspace = tmp_path / ".zicato"
    _make_generations(workspace, "e0")

    assert generation_phase.current_generation(workspace, "e0") == "v10"
    assert _latest_generation_id(workspace, "e0") == "v10"
    assert generation_phase.next_generation_id(workspace, "e0") == "v11"


def test_both_minters_agree_on_the_next_id(tmp_path: Path) -> None:
    """The evolve loop mints from the directory, ``zicato propose`` from a
    listing it already read; one rule answers for both, and a directory
    outside the ``vN`` scheme changes neither answer."""
    from zicato.cli.commands.propose import _list_generations
    from zicato.evolve import generation_phase

    workspace = tmp_path / ".zicato"
    _make_generations(workspace, "e0", [*ELEVEN, "named"])

    listing = _list_generations(workspace, "e0")
    assert listing == ["named", *ELEVEN]
    assert next_generation_id(listing) == generation_phase.next_generation_id(workspace, "e0")
    assert next_generation_id(listing) == "v11"
    assert next_generation_id([]) == "v0"
