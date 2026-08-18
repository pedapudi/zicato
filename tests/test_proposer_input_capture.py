"""Tests for the durable capture of every rendered proposer input.

Covers issue #244: one append-only ``epochs/{epoch_id}/proposer_inputs.jsonl``
per epoch, written before each proposer LLM call.

* the captured text is byte-identical to what the callable received;
* every retry attempt lands its own record, including a round that
  exhausts its retries;
* all four call sites capture — the text shim, the default ADK agent, the
  best-of-N self-critique, and the LLM recombination merge;
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
from zicato.proposer.input_capture import (
    ROLE_CRITIQUE,
    ROLE_PROPOSAL,
    ROLE_RECOMBINE_MERGE,
    capture_proposer_input,
    read_proposer_inputs,
)
from zicato.proposer.proposer import ProposerError, propose_experiment

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


async def _propose(workspace_root: Path | None, stub: _StubLLM, **overrides: Any) -> Experiment:
    kwargs: dict[str, Any] = {
        "epoch_id": _EPOCH,
        "parent_generation_id": "v0",
        "new_generation_id": "v1",
        "patterns": (),
        "mutations": _MUTATIONS,
        "brief_text": "# Proposer brief\n- Be careful.\n",
        "current_loss_summary": "loss=2.3, pass_rate=0.6",
        "aux_call_llm": stub,
        "model": "test-model",
        "workspace_root": workspace_root,
    }
    kwargs.update(overrides)
    return await propose_experiment(**kwargs)


# ---------------------------------------------------------------------------
# The text shim — byte identity, per-attempt records
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capture_is_byte_identical_to_what_the_callable_received(tmp_path: Path) -> None:
    stub = _StubLLM([_valid_response()])
    await _propose(tmp_path, stub)

    records = _records(tmp_path)
    assert len(records) == 1
    system, user, model = stub.calls[0]
    assert records[0]["system"] == system
    assert records[0]["user"] == user
    assert records[0]["model"] == model
    assert records[0]["role"] == ROLE_PROPOSAL
    assert records[0]["epoch_id"] == _EPOCH
    assert records[0]["parent_generation_id"] == "v0"
    assert records[0]["new_generation_id"] == "v1"
    assert records[0]["attempt"] == 0
    assert records[0]["ts"].endswith("Z")


@pytest.mark.asyncio
async def test_capture_lands_at_the_canonical_per_epoch_path(tmp_path: Path) -> None:
    stub = _StubLLM([_valid_response()])
    await _propose(tmp_path, stub)

    path = tmp_path / "epochs" / _EPOCH / "proposer_inputs.jsonl"
    assert path == proposer_inputs_path(tmp_path, _EPOCH)
    assert path.read_text(encoding="utf-8").endswith("\n")


@pytest.mark.asyncio
async def test_an_outer_workspace_root_descends_into_the_inner_tree(tmp_path: Path) -> None:
    (tmp_path / ".zicato" / "epochs").mkdir(parents=True)
    stub = _StubLLM([_valid_response()])
    await _propose(tmp_path, stub)

    assert (tmp_path / ".zicato" / "epochs" / _EPOCH / "proposer_inputs.jsonl").exists()
    assert not (tmp_path / "epochs").exists()


@pytest.mark.asyncio
async def test_every_retry_attempt_lands_its_own_record(tmp_path: Path) -> None:
    stub = _StubLLM(["not json at all", _valid_response()])
    await _propose(tmp_path, stub)

    records = _records(tmp_path)
    assert [r["attempt"] for r in records] == [0, 1]
    # The repair turn is what distinguishes the second prompt: it echoes the
    # prior raw output back, so the two user texts are NOT the same string.
    assert records[0]["user"] != records[1]["user"]
    assert [r["user"] for r in records] == [c[1] for c in stub.calls]


@pytest.mark.asyncio
async def test_a_round_that_exhausts_its_retries_still_leaves_every_prompt(
    tmp_path: Path,
) -> None:
    stub = _StubLLM(["nope"] * 3)
    with pytest.raises(ProposerError):
        await _propose(tmp_path, stub, max_retries=2)

    records = _records(tmp_path)
    assert [r["attempt"] for r in records] == [0, 1, 2]
    assert [r["user"] for r in records] == [c[1] for c in stub.calls]


@pytest.mark.asyncio
async def test_no_workspace_root_captures_nothing(tmp_path: Path) -> None:
    stub = _StubLLM([_valid_response()])
    await _propose(None, stub)
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# The default ADK path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_adk_agent_captures_its_task_text(tmp_path: Path) -> None:
    pytest.importorskip("google.adk")
    from google.adk.agents import LlmAgent

    from zicato.proposer.adk_agent import ADKProposerAgent
    from zicato.testing.adk_fake import TextTurn, make_fake_adk_model

    model = make_fake_adk_model([TextTurn(text=_valid_response())], model="proposer-model")
    agent = LlmAgent(name="capturing_proposer", model=model, instruction="emit JSON", tools=[])
    ctx = ProposerContext(
        epoch_id=_EPOCH,
        parent_generation_id="v0",
        new_generation_id="v1",
        patterns=(),
        mutations=_MUTATIONS,
        brief_text="# Proposer brief\n",
        current_loss_summary="loss=2.3",
        aux_call_llm=_unused_callable,
        model="aux-model",
        workspace_root=tmp_path,
        generation_root=tmp_path / "snapshot",
    )
    await ADKProposerAgent(spec=ProposerSpec.default(), agent=agent).propose(ctx)

    records = _records(tmp_path)
    assert len(records) == 1
    assert records[0]["role"] == ROLE_PROPOSAL
    assert records[0]["attempt"] == 0
    # The agent owns its static instruction, so only the task text is ours.
    assert records[0]["system"] == ""
    assert "# Proposer brief" in records[0]["user"]
    assert "loss=2.3" in records[0]["user"]


async def _unused_callable(*_a: Any, **_k: Any) -> str:  # pragma: no cover - never invoked
    raise AssertionError("the auxiliary callable must never be invoked on the ADK path")


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
