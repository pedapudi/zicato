"""``zicato proposer propose`` — the same episode, and nothing of the round.

The command exists so an operator can see what the proposer does with a
workspace's current evidence without spending a tournament on it. Two
things must be true for that to be worth anything, and both are pinned
here.

It must be the SAME proposal. The command builds its request through
:func:`zicato.proposer.foe_request.build_request` because it drives the
agent the round drives — so the charter, the epoch's brief and skills,
the mutation manifest and the loss summary reach the episode exactly as a
round's would, and a request an operator debugs is a request the loop
runs.

And it must leave the loop alone. Debugging must not write tournament
evidence, must not consume holdout data, and must not contaminate a
canonical cache — otherwise reading the proposer would change what the
next round measures.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from tests._contract_pins import deterministic_weights
from tests._foe_support import stand_in_proposer_block
from tests._source_tree_builders import mutable_tree
from zicato.cli.commands.propose import propose_cmd
from zicato.epoch.lifecycle import new_epoch

#: A board whose one holdout-tagged entry carries a phrase that appears
#: nowhere else, so its presence anywhere in the episode's context is
#: unambiguous.
_HOLDOUT_PHRASE = "quarantined-holdout-entry-text"


def _board(path: Path) -> None:
    entries = [
        {
            "id": "entry_train",
            "kind": "single_turn",
            "wall_clock_budget_seconds": 60,
            "input": "hello from the train slice",
        },
        {
            "id": "entry_held",
            "kind": "single_turn",
            "wall_clock_budget_seconds": 60,
            "input": _HOLDOUT_PHRASE,
            "tags": ["holdout"],
        },
    ]
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")


def _workspace(tmp_path: Path, **proposer: Any) -> tuple[Path, str]:
    """A registered workspace with one epoch, a v0 snapshot, and a proposer."""
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    agent = tmp_path / "agent"
    mutable_tree(agent, instr="Route the message.")
    (workspace / "config.json").write_text(
        json.dumps(
            {
                "instance_id": "test",
                "created_at": "2026-09-01T00:00:00Z",
                "generation_source_backend": "directory",
                "adapter": {"kind": "stub"},
                "source_roots": [str(agent)],
                "mutable_trees": [str(agent)],
                "proposer": stand_in_proposer_block(tmp_path / "foe", **proposer),
            }
        ),
        encoding="utf-8",
    )

    board_src = tmp_path / "board.jsonl"
    _board(board_src)
    brief_src = tmp_path / "brief.md"
    brief_src.write_text(
        "# Proposer brief\n- Keep the routing instruction terse.\n", encoding="utf-8"
    )
    cfg = new_epoch(
        workspace,
        name="propose",
        board_source=board_src,
        brief_source=brief_src,
        weights=deterministic_weights(promote_margin=0.01),
        auto_close_previous=False,
    )

    snapshot = workspace / "epochs" / cfg.id / "generations" / "v0" / "snapshot"
    mutable_tree(snapshot, instr="Route the message.")
    return workspace, cfg.id


def _run(workspace: Path, *args: str) -> Any:
    return CliRunner().invoke(propose_cmd, ["--workspace", str(workspace), *args])


def _tree(root: Path) -> dict[str, bytes]:
    """Every file under ``root``, by relative path — a comparable snapshot."""
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


def test_the_command_writes_the_experiment_its_episode_produced(tmp_path: Path) -> None:
    workspace, epoch_id = _workspace(tmp_path)

    result = _run(workspace)

    assert result.exit_code == 0, result.output
    written = workspace / "epochs" / epoch_id / "proposals" / "v1.json"
    assert written.is_file()
    body = json.loads(written.read_text(encoding="utf-8"))
    assert body["parent_generation_id"] == "v0"
    assert body["generation_id"] == "v1"
    assert [p["mutation_id"] for p in body["patches"]] == ["instr"]
    assert str(written) in result.output


def test_the_episode_is_given_the_round_s_authorized_context(tmp_path: Path) -> None:
    """The brief, the charter and the manifest reach the model, as a round's do."""
    workspace, epoch_id = _workspace(tmp_path)

    assert _run(workspace).exit_code == 0

    from zicato.proposer.input_capture import ROLE_PROPOSAL, read_proposer_inputs

    records = [r for r in read_proposer_inputs(workspace, epoch_id) if r["role"] == ROLE_PROPOSAL]
    assert len(records) == 1
    instructions, task = records[0]["system"], records[0]["user"]
    # The charter and the epoch's own brief — the request builder's work.
    assert "Only the declared mutation points may change." in instructions
    assert "Keep the routing instruction terse." in instructions
    # The round's evidence blocks, in the task.
    assert "## Mutation points" in task
    assert "id=instr" in task


def test_no_board_entry_reaches_the_episode(tmp_path: Path) -> None:
    """Not the holdout, and not the train slice either: no entry is shown."""
    workspace, epoch_id = _workspace(tmp_path)

    assert _run(workspace).exit_code == 0

    from zicato.proposer.input_capture import read_proposer_inputs

    seen = "\n".join(r["system"] + r["user"] for r in read_proposer_inputs(workspace, epoch_id))
    assert _HOLDOUT_PHRASE not in seen
    assert "hello from the train slice" not in seen
    assert "entry_held" not in seen
    assert "entry_train" not in seen


def test_debugging_records_no_tournament_evidence(tmp_path: Path) -> None:
    """No lineage entry, no tournament, no outcome, no generation minted."""
    workspace, epoch_id = _workspace(tmp_path)
    epoch = workspace / "epochs" / epoch_id

    assert _run(workspace).exit_code == 0

    from zicato.runtime.state import read_active_tournament

    assert read_active_tournament(workspace) is None
    assert not (epoch / "generations" / "v1").exists()
    assert not (epoch / "current_generation").exists()
    assert not (epoch / "rounds").exists()
    # The epoch's lineage is written when the epoch opens; what matters is
    # that debugging added no generation to it.
    lineage = json.loads((workspace / "lineage.json").read_text(encoding="utf-8"))
    row = next(e for e in lineage["epochs"] if e["id"] == epoch_id)
    assert [g["id"] for g in row.get("generations", [])] == []
    # The proposal itself carries no outcome to be mistaken for a verdict.
    body = json.loads((epoch / "proposals" / "v1.json").read_text(encoding="utf-8"))
    assert body["outcome"] is None


def test_debugging_leaves_the_canonical_trees_byte_identical(tmp_path: Path) -> None:
    """Everything the loop reads is untouched but the proposals directory."""
    workspace, epoch_id = _workspace(tmp_path)
    epoch = workspace / "epochs" / epoch_id
    before = _tree(workspace)

    assert _run(workspace).exit_code == 0

    after = _tree(workspace)
    added = set(after) - set(before)
    changed = {k for k in set(after) & set(before) if after[k] != before[k]}
    assert not changed, sorted(changed)
    # The one write is the proposal, beside the record of what produced it.
    assert added == {
        str((epoch / "proposals" / "v1.json").relative_to(workspace)),
        str((epoch / "proposer_inputs.jsonl").relative_to(workspace)),
    } | {a for a in added if a.startswith(str(Path("epochs") / epoch_id / "episodes"))}


def test_re_running_overwrites_only_its_own_last_answer(tmp_path: Path) -> None:
    """A second run is idempotent where it matters: one proposal per candidate."""
    workspace, epoch_id = _workspace(tmp_path)

    assert _run(workspace).exit_code == 0
    assert _run(workspace).exit_code == 0

    proposals = sorted((workspace / "epochs" / epoch_id / "proposals").iterdir())
    assert [p.name for p in proposals] == ["v1.json"]


def test_a_workspace_with_no_proposer_block_is_refused(tmp_path: Path) -> None:
    """The command names the block to add rather than choosing a proposer."""
    workspace, _ = _workspace(tmp_path)
    config_path = workspace / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    del config["proposer"]
    config_path.write_text(json.dumps(config), encoding="utf-8")

    result = _run(workspace)

    assert result.exit_code != 0
    assert "declares no `proposer` block" in result.output


def test_a_blocked_episode_is_reported_rather_than_written(tmp_path: Path) -> None:
    workspace, epoch_id = _workspace(tmp_path, refuse="goal-unreachable")

    result = _run(workspace)

    assert result.exit_code != 0
    assert not (workspace / "epochs" / epoch_id / "proposals").exists()


@pytest.mark.parametrize("flag", ["--epoch", "--max-retries"])
def test_the_documented_flags_are_accepted(tmp_path: Path, flag: str) -> None:
    workspace, epoch_id = _workspace(tmp_path)
    value = epoch_id if flag == "--epoch" else "0"

    assert _run(workspace, flag, value).exit_code == 0
