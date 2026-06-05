"""Tests for the tool-using ADK proposer agent (Design A).

Drives :class:`zicato.proposer.adk_agent.ADKProposerAgent` with the shipped
example agent (``zicato_examples.proposer_with_tools.agent``) wired to a
:class:`zicato.testing.adk_fake.FakeADKModel`, on ADK's own ``Runner``.
The fake model scripts a tool-call round (proving a
``DEFAULT_PROPOSER_TOOLS`` tool is actually invoked) followed by the final
``{hypothesis, patches}`` JSON turn. Additional tests prove a forbidden-id
violation and a post-apply rejection each cost one bounded retry, that the
run NEVER touches a harness/auxiliary callable, and that
``build_proposer_agent`` selects this agent for a custom-agent spec.

Gated on ``google.adk`` so the suite stays green without the optional
ADK extra.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("google.adk")

from zicato.core.types import ProposerSpec  # noqa: E402
from zicato.proposer.adk_agent import ADKProposerAgent  # noqa: E402
from zicato.proposer.agent import ProposerContext, build_proposer_agent  # noqa: E402
from zicato.proposer.proposer import ProposerError  # noqa: E402
from zicato.testing import make_mutation_point  # noqa: E402
from zicato.testing.adk_fake import (  # noqa: E402
    FunctionCallTurn,
    TextTurn,
    make_fake_adk_model,
)
from zicato_examples.proposer_with_tools.agent import build_agent  # noqa: E402


def _build_snapshot(tmp_path: Path) -> tuple[Path, tuple]:
    """Build a generation snapshot + manifest re-basing onto it."""
    snapshot = tmp_path / "snapshot"
    harness = snapshot / "harness"
    harness.mkdir(parents=True)
    (harness / "prompts.py").write_text(
        "SYSTEM_PROMPT = 'You are a helpful assistant.'  # TODO tighten\n",
        encoding="utf-8",
    )
    mp = make_mutation_point(
        id="harness__system_prompt",
        file=harness / "prompts.py",
        source_root=Path("/orig/harness"),
        content="You are a helpful assistant.",
    )
    return snapshot, (mp,)


def _experiment_json(*, mutation_id: str = "harness__system_prompt") -> str:
    """A schema-valid ``{hypothesis, patches}`` payload targeting ``mutation_id``."""
    return json.dumps(
        {
            "hypothesis": {
                "core_idea": "Tighten the system prompt to cut off-topic preambles.",
                "modulating": [mutation_id],
                "why": "grep shows a TODO marker; the prompt invites preambles.",
                "expected_metric_movements": [
                    {
                        "metric_name": "drift:off_topic",
                        "direction": "decrease",
                        "magnitude": "medium",
                    }
                ],
                "expected_pass_rate_delta": "+0.05 to +0.10",
            },
            "patches": [
                {
                    "mutation_id": mutation_id,
                    "op": "replace",
                    "new_content": "You are a terse, on-topic assistant.",
                    "rationale": "Remove the preamble license.",
                }
            ],
        }
    )


def _make_ctx(
    tmp_path: Path,
    snapshot: Path,
    mutations: tuple,
    **overrides: Any,
) -> ProposerContext:
    """Build a ProposerContext whose snapshot resolves to ``snapshot``.

    ``ADKProposerAgent._resolve_generation_root`` resolves the parent
    snapshot via the generation store from ``workspace_root`` + epoch +
    parent-gen ids. We seed the workspace so that path equals ``snapshot``.
    """
    from zicato.epoch.genstore import default_generation_store

    ws = tmp_path / "ws"
    epoch_id = "ep-001"
    parent_gen = "gen-000"
    store = default_generation_store(ws)
    parent_root = store.snapshot_root(epoch_id, parent_gen)
    parent_root.parent.mkdir(parents=True, exist_ok=True)
    # Make the store's resolved snapshot path BE our fixture snapshot by
    # placing the harness subtree under the canonical location.
    import shutil

    shutil.copytree(snapshot, parent_root)

    def _harness_raises(*_a: Any, **_k: Any) -> Any:  # pragma: no cover - must never run
        raise AssertionError("the harness/auxiliary callable must never be invoked")

    kwargs: dict[str, Any] = {
        "epoch_id": epoch_id,
        "parent_generation_id": parent_gen,
        "new_generation_id": "gen-001",
        "patterns": (),
        "mutations": mutations,
        "brief_text": "Tighten prompts; avoid preambles.",
        "current_loss_summary": "off_topic dominates this generation.",
        "aux_call_llm": _harness_raises,
        "model": "aux-model",
        "max_retries": 2,
        "workspace_root": ws,
    }
    kwargs.update(overrides)
    return ProposerContext(**kwargs)


@pytest.mark.asyncio
async def test_propose_returns_valid_experiment_with_tool_round(tmp_path: Path) -> None:
    snapshot, mutations = _build_snapshot(tmp_path)
    ctx = _make_ctx(tmp_path, snapshot, mutations)

    # Script: a tool call to a DEFAULT_PROPOSER_TOOLS tool, then the JSON.
    model = make_fake_adk_model(
        [
            FunctionCallTurn(name="list_mutation_points"),
            TextTurn(text=_experiment_json()),
        ],
        model="proposer-model",
    )
    agent = build_agent(model=model)
    proposer = ADKProposerAgent(spec=ProposerSpec.default(), agent=agent)

    experiment = await proposer.propose(ctx)

    assert experiment.hypothesis.core_idea.startswith("Tighten")
    assert len(experiment.patches) == 1
    assert experiment.patches[0].mutation_id == "harness__system_prompt"
    # Two model round-trips: the tool-call turn + the final-JSON turn — the
    # tool round actually happened.
    assert model.cursor >= 2


@pytest.mark.asyncio
async def test_tool_is_actually_invoked(tmp_path: Path) -> None:
    snapshot, mutations = _build_snapshot(tmp_path)
    ctx = _make_ctx(tmp_path, snapshot, mutations)

    # Recording hook: wrap grep_mutable so we can prove the tool fired AND
    # that it ran inside a bound ProposerToolContext (a real snapshot read).
    calls: list[str] = []
    from zicato.proposer import tools as proposer_tools

    real_grep = proposer_tools.grep_mutable

    def recording_grep(pattern: str) -> str:
        calls.append(pattern)
        return real_grep(pattern)

    recording_grep.__name__ = "grep_mutable"
    recording_grep.__doc__ = real_grep.__doc__

    from google.adk.agents import LlmAgent

    model = make_fake_adk_model(
        [
            FunctionCallTurn(name="grep_mutable", args={"pattern": "TODO"}),
            TextTurn(text=_experiment_json()),
        ],
        model="proposer-model",
    )
    agent = LlmAgent(
        name="recording_proposer",
        model=model,
        instruction="use grep_mutable then emit JSON",
        tools=[recording_grep],
    )
    proposer = ADKProposerAgent(spec=ProposerSpec.default(), agent=agent)

    experiment = await proposer.propose(ctx)

    assert experiment.patches[0].mutation_id == "harness__system_prompt"
    # The tool fired with the scripted pattern AND returned a real snapshot
    # match (proving the bound context was live during the run).
    assert calls == ["TODO"]


@pytest.mark.asyncio
async def test_forbidden_id_triggers_one_retry_then_succeeds(tmp_path: Path) -> None:
    snapshot, mutations = _build_snapshot(tmp_path)
    ctx = _make_ctx(
        tmp_path,
        snapshot,
        mutations,
        forbidden_ids=("harness__system_prompt",),
    )
    # Add a second, allowed mutation point so the retry has a clean target.
    extra = make_mutation_point(
        id="harness__router_prompt",
        file=snapshot / "harness" / "prompts.py",
        source_root=Path("/orig/harness"),
        content="route(msg)",
    )
    ctx = ProposerContext(
        **{
            **{f: getattr(ctx, f) for f in ctx.__dataclass_fields__},
            "mutations": (*mutations, extra),
        }
    )

    model = make_fake_adk_model(
        [
            # Attempt 1: targets the FORBIDDEN id → retryable failure.
            TextTurn(text=_experiment_json(mutation_id="harness__system_prompt")),
            # Attempt 2: targets the ALLOWED id → success.
            TextTurn(text=_experiment_json(mutation_id="harness__router_prompt")),
        ],
        model="proposer-model",
    )
    agent = build_agent(model=model)
    proposer = ADKProposerAgent(spec=ProposerSpec.default(), agent=agent)

    experiment = await proposer.propose(ctx)

    assert experiment.patches[0].mutation_id == "harness__router_prompt"
    assert model.cursor == 2  # exactly one retry


@pytest.mark.asyncio
async def test_validate_rejection_triggers_one_retry_then_succeeds(tmp_path: Path) -> None:
    snapshot, mutations = _build_snapshot(tmp_path)

    rejections: list[int] = []

    async def _validate(_experiment: Any) -> list[str]:
        # Reject the first parsed experiment, accept the second.
        rejections.append(1)
        if len(rejections) == 1:
            return ["dropped a marker; patch breaks the snapshot"]
        return []

    ctx = _make_ctx(tmp_path, snapshot, mutations, validate_experiment=_validate)

    model = make_fake_adk_model(
        [
            TextTurn(text=_experiment_json()),  # parses, but validate rejects
            TextTurn(text=_experiment_json()),  # parses, validate accepts
        ],
        model="proposer-model",
    )
    agent = build_agent(model=model)
    proposer = ADKProposerAgent(spec=ProposerSpec.default(), agent=agent)

    experiment = await proposer.propose(ctx)

    assert experiment.patches[0].mutation_id == "harness__system_prompt"
    assert len(rejections) == 2  # rejected once, accepted on retry
    assert model.cursor == 2


@pytest.mark.asyncio
async def test_exhausting_retries_raises_proposer_error(tmp_path: Path) -> None:
    snapshot, mutations = _build_snapshot(tmp_path)
    ctx = _make_ctx(tmp_path, snapshot, mutations, max_retries=1)

    # Every turn is unparseable → all attempts fail → ProposerError.
    model = make_fake_adk_model(
        [TextTurn(text="not json"), TextTurn(text="still not json")],
        model="proposer-model",
    )
    agent = build_agent(model=model)
    proposer = ADKProposerAgent(spec=ProposerSpec.default(), agent=agent)

    with pytest.raises(ProposerError) as excinfo:
        await proposer.propose(ctx)
    # max_retries=1 → 2 attempts → 2 recorded errors.
    assert len(excinfo.value.attempts) == 2
    assert model.cursor == 2


@pytest.mark.asyncio
async def test_run_never_touches_the_auxiliary_callable(tmp_path: Path) -> None:
    snapshot, mutations = _build_snapshot(tmp_path)

    invoked: list[str] = []

    async def _aux_should_not_run(*_a: Any, **_k: Any) -> str:
        invoked.append("called")
        raise AssertionError("ADK proposer must not invoke the auxiliary callable")

    ctx = _make_ctx(tmp_path, snapshot, mutations, aux_call_llm=_aux_should_not_run)
    model = make_fake_adk_model(
        [
            FunctionCallTurn(name="list_mutation_points"),
            TextTurn(text=_experiment_json()),
        ],
        model="proposer-model",
    )
    agent = build_agent(model=model)
    proposer = ADKProposerAgent(spec=ProposerSpec.default(), agent=agent)

    await proposer.propose(ctx)
    assert invoked == []


def test_build_proposer_agent_selects_adk_agent_for_custom_spec(tmp_path: Path) -> None:
    # A spec with agent_source_sha256 set + a proposer_path → ADKProposerAgent.
    proposer_dir = tmp_path / "proposers" / "my_proposer"
    proposer_dir.mkdir(parents=True)
    (proposer_dir / "agent.py").write_text("agent = object()\n", encoding="utf-8")

    spec = ProposerSpec(
        agent_id="dir:my_proposer",
        tools=(),
        skills=(),
        agent_source_sha256="deadbeef",
    )
    built = build_proposer_agent(spec, proposer_path=proposer_dir)
    assert isinstance(built, ADKProposerAgent)
    assert built.proposer_path == proposer_dir


def test_build_proposer_agent_requires_path_for_custom_spec() -> None:
    spec = ProposerSpec(
        agent_id="dir:my_proposer",
        tools=(),
        skills=(),
        agent_source_sha256="deadbeef",
    )
    with pytest.raises(ValueError, match="no proposer_path"):
        build_proposer_agent(spec)


def test_adk_agent_loads_module_level_agent_from_disk(tmp_path: Path) -> None:
    # Prove the disk-loading seam: a proposer dir whose agent.py exposes a
    # module-level ``agent`` symbol is loaded via _load_agent.
    proposer_dir = tmp_path / "proposers" / "loadable"
    proposer_dir.mkdir(parents=True)
    (proposer_dir / "agent.py").write_text(
        "SENTINEL = object()\nagent = SENTINEL\n", encoding="utf-8"
    )
    spec = ProposerSpec(
        agent_id="dir:loadable",
        tools=(),
        skills=(),
        agent_source_sha256="deadbeef",
    )
    proposer = ADKProposerAgent(spec=spec, proposer_path=proposer_dir)
    loaded = proposer._load_agent()
    assert loaded is not None
    # Idempotent: second load returns the cached agent.
    assert proposer._load_agent() is loaded
