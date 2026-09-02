"""One proposal, run as a Foe episode against the stand-in binary.

These are the proofs the replacement rests on: a completed episode's edits
become the experiment's patches, each of the other three endings reaches
the round as its own outcome, the episode's process is registered where
the watchdog can reach it and is ended when it outlives its budget, the
request the model sees carries no board entry, and the working copy is
gone whatever happened.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest

from tests._foe_support import (
    block_turn,
    call_turn,
    error_turn,
    fake_foe_binary,
    offered_tools,
    request_texts,
    return_turn,
    scripted_transport,
)
from tests._source_tree_builders import mutable_tree
from zicato.core.types import MutationPoint, ProposerSpec
from zicato.mutation.enumerator import enumerate_mutations
from zicato.proposer import foe_agent
from zicato.proposer.agent import ProposerContext
from zicato.proposer.external import external_proposer_config
from zicato.proposer.foe_agent import FoeProposerAgent
from zicato.proposer.foe_request import SANCTIONED_TOOLS
from zicato.proposer.foe_scratch import SCRATCH_PREFIX
from zicato.proposer.proposer import ProposerBlocked, ProposerError, ProposerExhausted
from zicato.runtime.state import list_active_runs

#: A snapshot with one declared mutation point, enumerated by the real
#: enumerator rather than described by hand: the projection and the patch
#: linter both re-enumerate the tree, so a hand-built manifest would prove
#: nothing about whether they agree.
_INSTR = "Route the message."


_HYPOTHESIS = {
    "core_idea": "Tell the router to answer with the agent name alone.",
    "modulating": ["instr"],
    "why": "The off-topic pattern dominates and the prompt invites preambles.",
    "expected_pass_rate_delta": "+0.05 to +0.10",
    "expected_metric_movements": [
        {"metric_name": "drift:off_topic", "direction": "decrease", "magnitude": "medium"}
    ],
}

#: What the model writes into the working copy. The whole file, because
#: the stand-in's edit tool replaces a file rather than splicing a range.
_EDITED_FILE = '\n# zicato:mutable id="instr"\nINSTR = """Answer with the agent name."""\n'


class Workspace:
    """A workspace with a snapshot, a manifest, and a configured proposer."""

    def __init__(self, tmp_path: Path, turns: list[dict[str, Any]], **binary: Any) -> None:
        self.root = tmp_path / "ws"
        self.root.mkdir()
        self.snapshot = tmp_path / "snapshot"
        self.tree = mutable_tree(self.snapshot, instr=_INSTR)
        self.binary = fake_foe_binary(tmp_path / "bin", **binary)
        transport = scripted_transport(tmp_path / "bin", turns)
        self.config = {
            "proposer": {
                "binary": str(self.binary),
                "budget": {"model_calls": 6, "seconds": 60},
                "model": {
                    "provider": "exec",
                    "model": "scripted",
                    "options": {"exec": str(transport)},
                },
            }
        }

    @property
    def points(self) -> tuple[MutationPoint, ...]:
        return tuple(enumerate_mutations([self.snapshot]))

    def agent(self) -> FoeProposerAgent:
        binding = external_proposer_config(self.config, self.root)
        assert binding is not None
        return FoeProposerAgent(
            spec=ProposerSpec(agent_id="external:foe", tools=(), skills=()),
            config=binding,
        )

    def context(self, **overrides: Any) -> ProposerContext:
        fields: dict[str, Any] = {
            "epoch_id": "e1",
            "parent_generation_id": "v0",
            "new_generation_id": "v1",
            "patterns": (),
            "mutations": self.points,
            "brief_text": "Reduce off-topic preambles.",
            "current_loss_summary": "drift loss 0.42",
            "aux_call_llm": _unused_callable,
            "workspace_root": self.root,
            "generation_root": self.snapshot,
        }
        fields.update(overrides)
        return ProposerContext(**fields)

    def episode_log(self) -> Path:
        return self.root / "epochs" / "e1" / "episodes" / "v1"


def _unused_callable(system: str, user: str, model: str) -> str:  # pragma: no cover
    raise AssertionError("a Foe episode never calls the auxiliary text shim")


def _edit(content: str, path: str = "agent/prompts.py") -> tuple[str, dict[str, Any]]:
    """One turn's edit of the working copy.

    The path is relative, which the stand-in resolves against the first
    write root — the copy — so a scripted turn names the file rather than
    the directory the host happened to mint for this episode.
    """
    return ("edit", {"path": path, "content": content})


def test_a_completed_episode_becomes_an_experiment_over_its_own_edits(tmp_path: Path) -> None:
    workspace = Workspace(
        tmp_path,
        [
            call_turn(_edit(_EDITED_FILE)),
            return_turn(_HYPOTHESIS),
        ],
    )
    experiment = asyncio.run(workspace.agent().propose(workspace.context()))

    assert experiment.hypothesis.core_idea == _HYPOTHESIS["core_idea"]
    assert [p.mutation_id for p in experiment.patches] == ["instr"]
    # The patch carries the applier's unit for a Python span — the literal
    # alone — so applying it back onto the snapshot reproduces the edit.
    assert experiment.patches[0].new_content == '"""Answer with the agent name."""'
    assert experiment.generation_id == "v1"
    assert experiment.parent_generation_id == "v0"


def test_the_snapshot_is_untouched_and_the_working_copy_is_gone(tmp_path: Path) -> None:
    workspace = Workspace(
        tmp_path,
        [
            call_turn(_edit(_EDITED_FILE)),
            return_turn(_HYPOTHESIS),
        ],
    )
    asyncio.run(workspace.agent().propose(workspace.context()))

    assert _INSTR in (workspace.tree / "prompts.py").read_text(encoding="utf-8")
    leftovers = list(Path(tmp_path).glob(f"**/{SCRATCH_PREFIX}*"))
    assert leftovers == []


def test_an_edit_outside_every_mutation_point_blocks_the_round(tmp_path: Path) -> None:
    workspace = Workspace(
        tmp_path,
        [
            call_turn(_edit("SNEAK = 1\n", path="agent/extra.py")),
            return_turn(_HYPOTHESIS),
        ],
    )
    with pytest.raises(ProposerBlocked) as raised:
        asyncio.run(workspace.agent().propose(workspace.context()))
    assert raised.value.code == "edit-outside-mutation-point"
    assert "extra.py" in raised.value.message


def test_an_episode_that_changed_nothing_blocks_rather_than_proposing(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path, [return_turn(_HYPOTHESIS)])
    with pytest.raises(ProposerBlocked) as raised:
        asyncio.run(workspace.agent().propose(workspace.context()))
    assert raised.value.code == "no-groundable-mutation-point"


def test_a_reported_block_reaches_the_round_under_its_zicato_code(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path, [block_turn("ambiguous-task", "the brief admits two readings")])
    with pytest.raises(ProposerBlocked) as raised:
        asyncio.run(workspace.agent().propose(workspace.context()))
    assert raised.value.code == "ambiguous-brief"
    assert raised.value.message == "the brief admits two readings"


def test_a_spent_budget_reaches_the_round_as_exhaustion(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path, [call_turn(("grep", {"pattern": "INSTR"}))])
    with pytest.raises(ProposerExhausted) as raised:
        asyncio.run(workspace.agent().propose(workspace.context()))
    assert raised.value.limit == "model_calls"


def test_a_transport_failure_reaches_the_round_as_a_failure(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path, [error_turn("the provider refused the request")])
    with pytest.raises(ProposerError) as raised:
        asyncio.run(workspace.agent().propose(workspace.context()))
    assert not isinstance(raised.value, ProposerBlocked | ProposerExhausted)
    assert "the provider refused the request" in str(raised.value)


def test_a_binary_that_dies_mid_episode_reaches_the_round_as_a_failure(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path, [return_turn(_HYPOTHESIS)], die_after=3)
    with pytest.raises(ProposerError) as raised:
        asyncio.run(workspace.agent().propose(workspace.context()))
    assert "before episode/end" in str(raised.value)


def test_the_episode_is_registered_where_the_watchdog_can_reach_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The record has to exist WHILE the episode runs, not after it."""
    workspace = Workspace(
        tmp_path,
        [
            call_turn(_edit(_EDITED_FILE)),
            return_turn(_HYPOTHESIS),
        ],
    )
    live: list[tuple[str, int]] = []
    real_remove = foe_agent._remove_active_run

    def observe(workspace_root: Path, run_id: str) -> None:
        live.extend((run.run_id, run.pid) for run in list_active_runs(workspace_root))
        real_remove(workspace_root, run_id)

    monkeypatch.setattr(foe_agent, "_remove_active_run", observe)
    asyncio.run(workspace.agent().propose(workspace.context()))

    assert [run_id for run_id, _pid in live] == ["propose:e1:v1"]
    assert live[0][1] > 0
    assert list_active_runs(workspace.root) == []


def test_an_episode_outliving_its_budget_is_ended_and_reported_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The host holding the pipe is the first to notice a missed deadline.

    The deadline is driven rather than waited out: the wait for the
    episode is made to time out at once, which is what a Foe process that
    did not honor its own `seconds` budget would look like from here.
    """
    workspace = Workspace(tmp_path, [return_turn(_HYPOTHESIS)])
    pids: list[int] = []
    real_register = foe_agent._register_active_run

    def observe(workspace_root: Path, run_id: str, handle: Any, *args: Any) -> None:
        pids.append(handle.pid)
        real_register(workspace_root, run_id, handle, *args)

    real_wait_for = asyncio.wait_for

    async def refuse_the_episode_wait(awaitable: Any, timeout: float | None = None) -> Any:
        if timeout == 60:
            awaitable.close()
            raise TimeoutError
        return await real_wait_for(awaitable, timeout)

    monkeypatch.setattr(foe_agent, "_register_active_run", observe)
    monkeypatch.setattr(asyncio, "wait_for", refuse_the_episode_wait)
    with pytest.raises(ProposerExhausted) as raised:
        asyncio.run(workspace.agent().propose(workspace.context()))

    assert raised.value.limit == "seconds"
    assert list_active_runs(workspace.root) == []
    with pytest.raises(ProcessLookupError):
        os.kill(pids[0], 0)


#: What an episode may do, written out here rather than imported. This
#: literal is the guard: comparing the running list against the production
#: constant would pass for any widening of that constant, which is the one
#: change this test exists to stop. ``return`` is the runtime's own
#: synthesis of the completion rule and belongs to no tool the contract
#: lists.
_EXPECTED_TOOLS = [
    "read",
    "grep",
    "edit",
    "block",
    "mutation_usage",
    "validate_patches",
    "return",
]


def test_the_running_tool_list_is_exactly_the_sanctioned_set(tmp_path: Path) -> None:
    """What the episode may do is what this test says, name by name.

    Read off the episode's own request header — the list the runtime told
    the MODEL about — so a widening has to change this file to land, and a
    reviewer sees the surface grow in the diff.
    """
    workspace = Workspace(
        tmp_path,
        [
            call_turn(_edit(_EDITED_FILE)),
            return_turn(_HYPOTHESIS),
        ],
    )
    asyncio.run(workspace.agent().propose(workspace.context()))

    assert offered_tools(workspace.episode_log()) == _EXPECTED_TOOLS
    # The production constant and the list above must agree; either alone
    # would let a widening through, so both are asserted.
    assert [*SANCTIONED_TOOLS, "return"] == _EXPECTED_TOOLS


def test_the_request_the_model_sees_carries_no_holdout_entry(tmp_path: Path) -> None:
    """Nothing board-shaped reaches the episode, on any surface.

    The evidence the round assembles is aggregate by construction, so the
    proof is total rather than a spot check: the board entry's id and its
    text appear nowhere in any request header, task item, or message the
    log recorded.
    """
    workspace = Workspace(
        tmp_path,
        [
            call_turn(_edit(_EDITED_FILE)),
            return_turn(_HYPOTHESIS),
        ],
    )
    asyncio.run(workspace.agent().propose(workspace.context()))

    shown = request_texts(workspace.episode_log())
    assert _INSTR in shown, "the mutation manifest must reach the model"
    for secret in ("holdout_entry_7", "What is the capital of Peru?"):
        assert secret not in shown


def test_the_episode_transcript_lands_under_the_epoch_s_records(tmp_path: Path) -> None:
    workspace = Workspace(
        tmp_path,
        [
            call_turn(_edit(_EDITED_FILE)),
            return_turn(_HYPOTHESIS),
        ],
    )
    asyncio.run(workspace.agent().propose(workspace.context()))
    events = (workspace.episode_log() / "episode.jsonl").read_text(encoding="utf-8")
    assert json.loads(events.splitlines()[0])["type"] == "episode/start"


def test_a_verifier_finding_costs_a_turn_rather_than_the_round(tmp_path: Path) -> None:
    """The findings go back to the model, and a second attempt can succeed."""
    workspace = Workspace(
        tmp_path,
        [
            return_turn(_HYPOTHESIS),
            call_turn(_edit(_EDITED_FILE)),
            return_turn(_HYPOTHESIS),
        ],
    )
    experiment = asyncio.run(workspace.agent().propose(workspace.context()))
    assert [p.mutation_id for p in experiment.patches] == ["instr"]

    verdicts = [
        json.loads(line)["data"]["value"]
        for line in (workspace.episode_log() / "episode.jsonl").read_text().splitlines()
        if json.loads(line)["type"] == "tool/result"
        and json.loads(line)["data"]["name"] == "validate_patches"
    ]
    assert verdicts[0] == [
        "the working copy is unchanged; change a declared mutation point "
        "before returning, or report a block"
    ]
    assert verdicts[-1] == []


def test_the_identity_names_foe_and_needs_no_credential(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path, [return_turn(_HYPOTHESIS)])
    binding = external_proposer_config(workspace.config, workspace.root)
    assert binding is not None
    identity = FoeProposerAgent.contract_identity(binding)
    assert identity["kind"] == "foe"
    assert str(identity["contract_fingerprint"]).startswith("sha256:")
    assert identity["tools"] == [
        "read",
        "grep",
        "edit",
        "block",
        "mutation_usage",
        "validate_patches",
    ]
