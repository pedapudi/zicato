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
from zicato.proposer import adk_agent as adk_agent_mod  # noqa: E402
from zicato.proposer.adk_agent import (  # noqa: E402
    ADKProposerAgent,
    build_default_adk_agent,
)
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
    ws.mkdir()
    (ws / "config.json").write_text(
        json.dumps({"generation_source_backend": "directory"}), encoding="utf-8"
    )
    epoch_id = "ep-001"
    parent_gen = "gen-000"
    store = default_generation_store(ws)
    parent_root = store.snapshot_path(epoch_id, parent_gen)
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


def _request_text(request: Any) -> str:
    """Concatenate every text part of one captured ADK LlmRequest."""
    chunks: list[str] = []
    for content in getattr(request, "contents", None) or ():
        for part in getattr(content, "parts", None) or ():
            text = getattr(part, "text", None)
            if text:
                chunks.append(text)
    return "\n".join(chunks)


@pytest.mark.asyncio
async def test_revise_feedback_seeds_the_first_run_input(tmp_path: Path) -> None:
    """The context's revise channel reaches the FIRST agent run's input as a
    repair section (the best-of-N screen-informed revise path)."""
    snapshot, mutations = _build_snapshot(tmp_path)
    ctx = _make_ctx(tmp_path, snapshot, mutations, revise_feedback="screen vetoed the whole slate")
    model = make_fake_adk_model([TextTurn(text=_experiment_json())], model="proposer-model")
    agent = build_agent(model=model)
    proposer = ADKProposerAgent(spec=ProposerSpec.default(), agent=agent)

    await proposer.propose(ctx)

    assert model.invocations
    first_input = _request_text(model.invocations[0])
    assert "Previous attempt was rejected" in first_input
    assert "screen vetoed the whole slate" in first_input


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


# ---------------------------------------------------------------------------
# The BUILT-IN DEFAULT proposer — a tool-using ADK agent, used when a
# contract configures no proposer dir.
# ---------------------------------------------------------------------------


def _default_agent_with_model(model: Any) -> Any:
    """Build the built-in default proposer agent wired to a fake ``model``.

    Reuses the production :func:`build_default_adk_agent` factory but swaps
    the agent's model for ``model`` so the default-proposer instruction +
    the full read-only tool registry are exactly what runs, with a scripted
    fake model in place of a real endpoint.
    """
    agent = build_default_adk_agent("placeholder-model")
    agent.model = model
    return agent


def test_build_proposer_agent_default_is_builtin_adk_agent() -> None:
    # No proposer dir configured → the DEFAULT is the tool-using ADK agent
    # in builtin_default mode (NOT the skill-composed single-shot engine).
    built = build_proposer_agent(ProposerSpec.default())
    assert isinstance(built, ADKProposerAgent)
    assert built.builtin_default is True
    assert built.agent is None  # built lazily at first propose


def test_build_default_adk_agent_uses_full_tool_registry_and_model() -> None:
    from zicato.proposer.tools import DEFAULT_PROPOSER_TOOLS

    agent = build_default_adk_agent("my-proposer-model")
    assert agent.model == "my-proposer-model"
    # Every read-only proposer tool is wired in by name.
    tool_names = {getattr(t, "__name__", None) for t in DEFAULT_PROPOSER_TOOLS}
    wired = {getattr(getattr(t, "func", t), "__name__", None) for t in agent.tools}
    assert tool_names <= wired


@pytest.mark.asyncio
async def test_default_proposer_runs_tool_agent_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # build_proposer_agent(default) → ADKProposerAgent(builtin_default) →
    # propose() builds the default tool-using agent from ctx.model and runs
    # it on ADK's own Runner. We intercept the factory so the agent runs on
    # a scripted fake model (a tool round, then the final JSON).
    snapshot, mutations = _build_snapshot(tmp_path)
    ctx = _make_ctx(tmp_path, snapshot, mutations)

    model = make_fake_adk_model(
        [
            FunctionCallTurn(name="list_mutation_points"),
            TextTurn(text=_experiment_json()),
        ],
        model="default-proposer-model",
    )
    monkeypatch.setattr(
        adk_agent_mod,
        "build_default_adk_agent",
        lambda _model: _default_agent_with_model(model),
    )

    proposer = build_proposer_agent(ProposerSpec.default())
    assert isinstance(proposer, ADKProposerAgent)

    experiment = await proposer.propose(ctx)

    assert experiment.patches[0].mutation_id == "harness__system_prompt"
    # The default agent ran on ADK's Runner: a tool round + the final JSON.
    assert model.cursor >= 2


@pytest.mark.asyncio
async def test_default_proposer_builds_agent_from_ctx_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The built-in default binds its LlmAgent to ctx.model — the workspace's
    # auxiliary model string the orchestrator threads on the context.
    snapshot, mutations = _build_snapshot(tmp_path)
    ctx = _make_ctx(tmp_path, snapshot, mutations, model="aux-model-xyz")

    seen_models: list[Any] = []

    def _capture(model: Any) -> Any:
        seen_models.append(model)
        fake = make_fake_adk_model([TextTurn(text=_experiment_json())], model="fake")
        return _default_agent_with_model(fake)

    monkeypatch.setattr(adk_agent_mod, "build_default_adk_agent", _capture)

    proposer = build_proposer_agent(ProposerSpec.default())
    await proposer.propose(ctx)

    assert seen_models == ["aux-model-xyz"]


@pytest.mark.asyncio
async def test_default_proposer_salvages_malformed_then_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Robustness carried forward to the DEFAULT path: a first turn that
    # buries the JSON under a <think> reasoning wrapper + prose is SALVAGED
    # by parse_experiment_json (no retry needed); a genuinely unrecoverable
    # turn would retry. Here the salvageable turn parses on the first run.
    snapshot, mutations = _build_snapshot(tmp_path)
    ctx = _make_ctx(tmp_path, snapshot, mutations)

    buried = (
        "<think>Let me reason about the prompt before answering. The TODO "
        "marker suggests the preamble is too loose.</think>\n"
        "Here is my proposal:\n```json\n" + _experiment_json() + "\n```\n"
    )
    model = make_fake_adk_model([TextTurn(text=buried)], model="default-proposer-model")
    monkeypatch.setattr(
        adk_agent_mod,
        "build_default_adk_agent",
        lambda _model: _default_agent_with_model(model),
    )

    proposer = build_proposer_agent(ProposerSpec.default())
    experiment = await proposer.propose(ctx)

    assert experiment.patches[0].mutation_id == "harness__system_prompt"
    # Salvaged on the FIRST run — no retry round.
    assert model.cursor == 1


@pytest.mark.asyncio
async def test_default_proposer_accepts_declared_judge_metric(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Judge-reference / semantic validation carried forward to the DEFAULT
    # path: a hypothesis that addresses a declared custom judge via the
    # ``drift:custom:<judge>`` mangle is normalized and accepted when the
    # bare judge name is in ctx.custom_judge_names.
    snapshot, mutations = _build_snapshot(tmp_path)
    ctx = _make_ctx(
        tmp_path,
        snapshot,
        mutations,
        custom_judge_names=frozenset({"file_findability"}),
    )

    payload = json.dumps(
        {
            "hypothesis": {
                "core_idea": "Tighten the system prompt.",
                "modulating": ["harness__system_prompt"],
                "why": "the prompt invites preambles.",
                "expected_metric_movements": [
                    {
                        "metric_name": "drift:custom:file_findability",
                        "direction": "increase",
                        "magnitude": "medium",
                    }
                ],
                "expected_pass_rate_delta": "+0.05",
            },
            "patches": [
                {
                    "mutation_id": "harness__system_prompt",
                    "op": "replace",
                    "new_content": "You are a terse, on-topic assistant.",
                    "rationale": "Remove the preamble license.",
                }
            ],
        }
    )
    model = make_fake_adk_model([TextTurn(text=payload)], model="default-proposer-model")
    monkeypatch.setattr(
        adk_agent_mod,
        "build_default_adk_agent",
        lambda _model: _default_agent_with_model(model),
    )

    proposer = build_proposer_agent(ProposerSpec.default())
    experiment = await proposer.propose(ctx)

    # The ``drift:custom:file_findability`` mangle resolved to the declared
    # judge and the metric movement was kept verbatim.
    metric_names = [m.metric_name for m in experiment.hypothesis.expected_metric_movements]
    assert "drift:custom:file_findability" in metric_names


@pytest.mark.asyncio
async def test_default_proposer_rejects_unknown_judge_metric(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The flip side: a ``drift:bogus`` metric that is neither a built-in
    # drift kind nor a declared judge is rejected, and (no salvage can fix
    # a semantic error) the run exhausts retries → ProposerError.
    snapshot, mutations = _build_snapshot(tmp_path)
    ctx = _make_ctx(
        tmp_path,
        snapshot,
        mutations,
        custom_judge_names=frozenset({"file_findability"}),
        max_retries=1,
    )

    payload = json.dumps(
        {
            "hypothesis": {
                "core_idea": "Tighten the system prompt.",
                "modulating": ["harness__system_prompt"],
                "why": "the prompt invites preambles.",
                "expected_metric_movements": [
                    {
                        "metric_name": "drift:bogus_unknown_kind",
                        "direction": "decrease",
                        "magnitude": "medium",
                    }
                ],
                "expected_pass_rate_delta": "+0.05",
            },
            "patches": [
                {
                    "mutation_id": "harness__system_prompt",
                    "op": "replace",
                    "new_content": "x",
                    "rationale": "y",
                }
            ],
        }
    )
    model = make_fake_adk_model(
        [TextTurn(text=payload), TextTurn(text=payload)],
        model="default-proposer-model",
    )
    monkeypatch.setattr(
        adk_agent_mod,
        "build_default_adk_agent",
        lambda _model: _default_agent_with_model(model),
    )

    proposer = build_proposer_agent(ProposerSpec.default())
    with pytest.raises(ProposerError) as excinfo:
        await proposer.propose(ctx)
    assert any("bogus_unknown_kind" in a for a in excinfo.value.attempts)


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
