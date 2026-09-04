"""The durable capture of every rendered proposer input.

One append-only ``epochs/{epoch_id}/proposer_inputs.jsonl`` per epoch,
written before each call it describes.

* a proposal episode lands one record carrying the exact instructions and
  the exact task the episode ran under;
* the other two call sites capture too — the best-of-N self-critique and
  the LLM recombination merge — each under its own role;
* concurrent writers produce one parseable line per call, never a spliced
  record;
* an unwritable workspace degrades to a no-op rather than an exception.
"""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tests._foe_support import stand_in_proposer_block
from tests._source_tree_builders import mutable_tree
from zicato.core.types import (
    Experiment,
    HypothesisSpec,
    MutationPoint,
    Patch,
    ProposerQualityConfig,
    ProposerSpec,
)
from zicato.core.workspace import proposer_inputs_path
from zicato.proposer.agent import ProposerContext
from zicato.proposer.best_of_n import BestOfNProposerAgent
from zicato.proposer.external import external_proposer_config
from zicato.proposer.foe_agent import FoeProposerAgent
from zicato.proposer.input_capture import (
    ROLE_CRITIQUE,
    ROLE_PROPOSAL,
    ROLE_RECOMBINE_MERGE,
    capture_proposer_input,
    read_proposer_inputs,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_EPOCH = "ep-001"


def _mp(mid: str) -> MutationPoint:
    return MutationPoint(
        id=mid,
        kind="span",
        file=Path(f"/src/{mid}.py"),
        source_root=Path("/src"),
        line_start=1,
        line_end=3,
        content="content",
        content_hash="abc",
        metadata={},
    )


_MUTATIONS = (_mp("router__sp"), _mp("writer__sp"))


def _valid_response() -> str:
    return json.dumps(
        {
            "hypothesis": {
                "core_idea": "tighten router preamble",
                "modulating": ["router__sp"],
                "why": "off_topic dominates",
                "expected_drift_movements": [
                    {"kind": "off_topic", "direction": "decrease", "magnitude": "medium"}
                ],
                "expected_pass_rate_delta": "+0.05",
            },
            "patches": [
                {
                    "mutation_id": "router__sp",
                    "op": "replace",
                    "new_content": "new router prompt",
                    "rationale": "tighter wording",
                }
            ],
        }
    )


class _StubLLM:
    """Records every call and returns scripted responses in order."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str, str]] = []

    async def __call__(self, system: str, user: str, model: str) -> str:
        self.calls.append((system, user, model))
        return self._responses.pop(0)


def _records(workspace_root: Path) -> list[dict[str, Any]]:
    return list(read_proposer_inputs(workspace_root, _EPOCH))


# ---------------------------------------------------------------------------
# The proposal episode — one record per episode, carrying its own context
# ---------------------------------------------------------------------------


def _episode_agent(tmp_path: Path) -> tuple[FoeProposerAgent, Path]:
    """A Foe-backed agent over the stand-in, and the snapshot it edits."""
    snapshot = tmp_path / "snapshot"
    mutable_tree(snapshot, instr="Route the message.")
    config = {"proposer": stand_in_proposer_block(tmp_path / "foe")}
    binding = external_proposer_config(config, tmp_path)
    assert binding is not None
    spec = ProposerSpec(agent_id="external:foe", tools=(), skills=())
    return FoeProposerAgent(spec=spec, config=binding), snapshot


def _episode_ctx(tmp_path: Path, snapshot: Path, **overrides: Any) -> ProposerContext:
    from zicato.mutation.enumerator import enumerate_mutations

    ctx = ProposerContext(
        epoch_id=_EPOCH,
        parent_generation_id="v0",
        new_generation_id="v1",
        patterns=(),
        mutations=tuple(enumerate_mutations([snapshot])),
        brief_text="# Proposer brief\n- Be careful.\n",
        current_loss_summary="loss=2.3, pass_rate=0.6",
        aux_call_llm=_unused_callable,
        model="test-model",
        workspace_root=tmp_path,
        generation_root=snapshot,
    )
    return replace(ctx, **overrides)


@pytest.mark.asyncio
async def test_an_episode_captures_the_context_it_ran_under(tmp_path: Path) -> None:
    agent, snapshot = _episode_agent(tmp_path)
    await agent.propose(_episode_ctx(tmp_path, snapshot))

    records = _records(tmp_path)
    assert len(records) == 1
    record = records[0]
    assert record["role"] == ROLE_PROPOSAL
    assert record["epoch_id"] == _EPOCH
    assert record["parent_generation_id"] == "v0"
    assert record["new_generation_id"] == "v1"
    assert record["ts"].endswith("Z")
    # The instructions carry the charter and the epoch's brief; the task
    # carries the round's evidence. Both are what the model was shown.
    assert "# Proposer brief" in record["system"]
    assert "Only the declared mutation points may change." in record["system"]
    assert "loss=2.3" in record["user"]
    assert "## Mutation points" in record["user"]


@pytest.mark.asyncio
async def test_capture_lands_at_the_canonical_per_epoch_path(tmp_path: Path) -> None:
    agent, snapshot = _episode_agent(tmp_path)
    await agent.propose(_episode_ctx(tmp_path, snapshot))

    path = tmp_path / "epochs" / _EPOCH / "proposer_inputs.jsonl"
    assert path == proposer_inputs_path(tmp_path, _EPOCH)
    assert path.read_text(encoding="utf-8").endswith("\n")


@pytest.mark.asyncio
async def test_each_slate_slot_lands_its_own_record(tmp_path: Path) -> None:
    """A slate's slots are separate episodes, so each records its own."""
    agent, snapshot = _episode_agent(tmp_path)
    for slot in (0, 1):
        await agent.propose(_episode_ctx(tmp_path, snapshot, slot_index=slot))

    records = _records(tmp_path)
    assert [r["slot"] for r in records] == [0, 1]
    # The slot is named in the task, so the two are not the same string.
    assert records[0]["user"] != records[1]["user"]


async def _unused_callable(*_a: Any, **_k: Any) -> str:  # pragma: no cover - never invoked
    raise AssertionError("the evaluation callable must never be invoked on the ADK path")


# ---------------------------------------------------------------------------
# The best-of-N calls: self-critique and the LLM recombination merge
# ---------------------------------------------------------------------------


def _experiment(core_idea: str, mutation_id: str) -> Experiment:
    return Experiment(
        id=f"exp_{core_idea}",
        epoch_id=_EPOCH,
        generation_id="v1",
        parent_generation_id="v0",
        proposed_at="2026-01-01T00:00:00Z",
        hypothesis=HypothesisSpec(
            core_idea=core_idea,
            modulating=(mutation_id,),
            why="because",
            expected_drift_movements=(),
            expected_pass_rate_delta="+0.01",
        ),
        patches=(
            Patch(
                id=f"p_{core_idea}",
                mutation_id=mutation_id,
                op="replace",
                new_content=core_idea,
                new_numeric=None,
                new_enum=None,
                rationale="r",
            ),
        ),
        outcome=None,
    )


class _ScriptedInner:
    """An inner proposer that returns scripted candidates, capturing nothing."""

    def __init__(self, candidates: list[Experiment]) -> None:
        self._candidates = candidates
        self.calls = 0
        self.slot_indices: list[int | None] = []

    async def propose(self, ctx: ProposerContext) -> Experiment:
        self.slot_indices.append(ctx.slot_index)
        idx = self.calls
        self.calls += 1
        return self._candidates[idx % len(self._candidates)]


def _bon_ctx(tmp_path: Path, aux: Any, **overrides: Any) -> ProposerContext:
    ctx = ProposerContext(
        epoch_id=_EPOCH,
        parent_generation_id="v0",
        new_generation_id="v1",
        patterns=(),
        mutations=_MUTATIONS,
        brief_text="# Proposer brief\n",
        current_loss_summary="loss=2.3, pass_rate=0.6",
        aux_call_llm=aux,
        model="test-model",
        workspace_root=tmp_path,
    )
    return replace(ctx, **overrides)


@pytest.mark.asyncio
async def test_the_self_critique_call_is_captured(tmp_path: Path) -> None:
    critic = _StubLLM(["0"])
    inner = _ScriptedInner(
        [_experiment("a", "router__sp"), _experiment("b", "writer__sp")],
    )
    agent = BestOfNProposerAgent(inner=inner, config=ProposerQualityConfig(best_of_n=2))
    await agent.propose(_bon_ctx(tmp_path, critic))

    records = _records(tmp_path)
    assert [r["role"] for r in records] == [ROLE_CRITIQUE]
    system, user, _ = critic.calls[0]
    assert records[0]["system"] == system
    assert records[0]["user"] == user
    # Every slate slot carries its own coordinate for the capture to record.
    assert inner.slot_indices == [0, 1]


@pytest.mark.asyncio
async def test_the_recombination_merge_call_is_captured(tmp_path: Path) -> None:
    from zicato.proposer.recombine import RecombinationPair

    pair = RecombinationPair(
        a_generation_id="v1",
        b_generation_id="v2",
        a_patches=(_experiment("a", "router__sp").patches[0],),
        b_patches=(_experiment("b", "writer__sp").patches[0],),
        a_core_idea="fix the router",
        b_core_idea="fix the writer",
        a_improved_count=1,
        b_improved_count=1,
        combined_improved_count=2,
        combined_regressed_count=0,
    )
    # Two slots: slot 0 samples, slot 1 is the recombination slot. The merge
    # response is unparseable, so the slot degrades — the capture still ran.
    aux = _StubLLM(["not an experiment", "0"])
    inner = _ScriptedInner([_experiment("a", "router__sp"), _experiment("b", "writer__sp")])
    agent = BestOfNProposerAgent(
        inner=inner,
        config=ProposerQualityConfig(best_of_n=2, recombine_merge="llm"),
    )
    await agent.propose(_bon_ctx(tmp_path, aux, recombine_pair=pair))

    records = _records(tmp_path)
    merges = [r for r in records if r["role"] == ROLE_RECOMBINE_MERGE]
    assert len(merges) == 1
    assert merges[0]["slot"] == 1
    assert merges[0]["system"] == aux.calls[0][0]
    assert merges[0]["user"] == aux.calls[0][1]
    assert "fix the router" in merges[0]["user"]
    assert [r["role"] for r in records] == [ROLE_RECOMBINE_MERGE, ROLE_CRITIQUE]


# ---------------------------------------------------------------------------
# Write mechanics: concurrency, degradation, tolerant reads
# ---------------------------------------------------------------------------


def test_concurrent_writers_never_splice_two_records(tmp_path: Path) -> None:
    """The lock + single ``O_APPEND`` write is a property of the WRITER.

    Today's slate serialises on the event-loop thread, so the threads here
    drive the writer directly — that is the contract being pinned, not the
    caller's current scheduling.
    """
    writers = 8
    per_writer = 12
    payload = "x" * 40_000  # comfortably past one buffered-write chunk

    def _write(worker: int) -> None:
        for i in range(per_writer):
            capture_proposer_input(
                workspace_root=tmp_path,
                epoch_id=_EPOCH,
                role=ROLE_PROPOSAL,
                system=f"system-{worker}",
                user=f"{worker}:{i}:{payload}",
                model="test-model",
                slot=worker,
            )

    threads = [threading.Thread(target=_write, args=(w,)) for w in range(writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = proposer_inputs_path(tmp_path, _EPOCH).read_text(encoding="utf-8").splitlines()
    assert len(lines) == writers * per_writer
    seen = set()
    for line in lines:
        record = json.loads(line)  # every line parses — no spliced record
        worker, index, body = record["user"].split(":", 2)
        assert body == payload  # ... and no record lost its tail to a sibling
        assert record["slot"] == int(worker)
        seen.add((worker, index))
    assert len(seen) == writers * per_writer


def test_an_unwritable_workspace_degrades_to_a_no_op(tmp_path: Path) -> None:
    epoch_dir = tmp_path / "epochs" / _EPOCH
    epoch_dir.mkdir(parents=True)
    epoch_dir.chmod(0o500)
    try:
        capture_proposer_input(
            workspace_root=tmp_path,
            epoch_id=_EPOCH,
            role=ROLE_PROPOSAL,
            system="s",
            user="u",
        )
        assert not (epoch_dir / "proposer_inputs.jsonl").exists()
    finally:
        epoch_dir.chmod(0o700)


def test_the_reader_tolerates_an_absent_file(tmp_path: Path) -> None:
    assert list(read_proposer_inputs(tmp_path, _EPOCH)) == []


def test_the_reader_skips_a_torn_final_line(tmp_path: Path) -> None:
    capture_proposer_input(
        workspace_root=tmp_path,
        epoch_id=_EPOCH,
        role=ROLE_PROPOSAL,
        system="s",
        user="u",
    )
    path = proposer_inputs_path(tmp_path, _EPOCH)
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"role":"proposal","user":"half a rec')

    records = _records(tmp_path)
    assert [r["user"] for r in records] == ["u"]


def test_the_reader_raises_on_interior_corruption(tmp_path: Path) -> None:
    for text in ("first", "second"):
        capture_proposer_input(
            workspace_root=tmp_path,
            epoch_id=_EPOCH,
            role=ROLE_PROPOSAL,
            system="s",
            user=text,
        )
    path = proposer_inputs_path(tmp_path, _EPOCH)
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("torn interior\n" + "\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="append-only invariant"):
        list(read_proposer_inputs(tmp_path, _EPOCH))
