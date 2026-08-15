from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from zicato.core.types import Experiment, MutationPoint, ProposerQualityConfig, ProposerSpec
from zicato.proposer.agent import ProposerContext
from zicato.proposer.best_of_n import NativeSlateAdapter, wrap_with_proposer_quality
from zicato.proposer.external import ExternalProposerConfig
from zicato.proposer.pi_agent import PiProposerAgent
from zicato.proposer.proposer import ProposerError

STUB = Path(__file__).parent / "_pi_stub_peer.py"

_MUTATIONS = (
    MutationPoint(
        id="router__sp",
        kind="span",
        file=Path("/src/router.py"),
        source_root=Path("/src"),
        line_start=1,
        line_end=3,
        content="content",
        content_hash="abc",
        metadata={},
    ),
)


def _experiment_args(mutation_id: str = "router__sp") -> dict[str, Any]:
    return {
        "hypothesis": {
            "core_idea": "tighten the router preamble",
            "modulating": [mutation_id],
            "why": "off_topic dominates",
            "expected_drift_movements": [
                {"kind": "off_topic", "direction": "decrease", "magnitude": "medium"}
            ],
            "expected_pass_rate_delta": "+0.05",
        },
        "patches": [
            {
                "mutation_id": mutation_id,
                "op": "replace",
                "new_content": "new router prompt",
                "rationale": "tighter wording",
            }
        ],
    }


def _candidate(name: str, content: str) -> dict[str, Any]:
    candidate = _experiment_args()
    candidate["hypothesis"]["core_idea"] = name
    candidate["hypothesis"]["why"] = f"because {name}"
    candidate["patches"][0]["new_content"] = content
    return candidate


async def _never_called(system: str, user: str, model: str) -> str:
    raise AssertionError("the pi tier must never fall back to the auxiliary text shim")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "epochs").mkdir()
    return tmp_path


@pytest.fixture
def records(tmp_path: Path) -> Path:
    return tmp_path / "stub-records"


@pytest.fixture
def agent(workspace: Path, records: Path, tmp_path: Path, monkeypatch: Any) -> PiProposerAgent:
    monkeypatch.setenv("ZICATO_PI_STUB_RECORD", str(records))
    monkeypatch.setenv("ZICATO_PI_STUB_SCRIPT", str(tmp_path / "script.json"))
    config = ExternalProposerConfig(
        dotted_path="zicato.proposer.pi_agent:PiProposerAgent",
        workspace_root=workspace,
        options={"pi_bin": str(STUB)},
    )
    return PiProposerAgent(spec=ProposerSpec.default(), config=config)


def _script(tmp_path: Path, turns: list[dict[str, Any]], model: str = "stub/model-1") -> None:
    (tmp_path / "script.json").write_text(
        json.dumps({"model": model, "turns": turns}), encoding="utf-8"
    )


def _context(workspace: Path, model: str = "stub/model-1", **overrides: Any) -> ProposerContext:
    fields: dict[str, Any] = {
        "epoch_id": "e1",
        "parent_generation_id": "v0",
        "new_generation_id": "v1",
        "patterns": (),
        "mutations": _MUTATIONS,
        "brief_text": "# Proposer brief\n- Be careful.\n",
        "current_loss_summary": "loss=2.3",
        "aux_call_llm": _never_called,
        "model": model,
        "workspace_root": workspace,
    }
    fields.update(overrides)
    return ProposerContext(**fields)


def _launches(records: Path) -> list[dict[str, Any]]:
    if not records.is_dir():
        return []
    found = sorted(records.glob("launch-*.json"))
    return [json.loads(path.read_text(encoding="utf-8")) for path in found]


@pytest.mark.asyncio
async def test_a_terminating_tool_call_becomes_an_experiment(
    agent: PiProposerAgent, workspace: Path, tmp_path: Path, records: Path
) -> None:
    _script(tmp_path, [{"emit": _experiment_args()}])

    experiment = await agent.propose(_context(workspace))

    assert isinstance(experiment, Experiment)
    assert [p.mutation_id for p in experiment.patches] == ["router__sp"]
    assert len(_launches(records)) == 1


@pytest.mark.asyncio
async def test_the_prompt_is_the_engine_prompt(
    agent: PiProposerAgent, workspace: Path, tmp_path: Path, records: Path
) -> None:
    _script(tmp_path, [{"emit": _experiment_args()}])

    await agent.propose(_context(workspace))

    prompt = _launches(records)[0]["prompts"][0]
    assert "router__sp" in prompt
    assert "loss=2.3" in prompt
    argv = _launches(records)[0]["argv"]
    assert "# Proposer brief" in argv[argv.index("--system-prompt") + 1]


@pytest.mark.asyncio
async def test_best_of_n_generation_and_review_share_one_session(
    agent: PiProposerAgent, workspace: Path, tmp_path: Path, records: Path
) -> None:
    candidates = [_candidate("small", "a"), _candidate("different", "b"), _candidate("best", "c")]
    _script(
        tmp_path,
        [{"emit": item} for item in candidates]
        + [{"select": {"index": 2, "rationale": "best grounded change"}}],
    )
    events: list[tuple[str, dict[str, Any]]] = []
    wrapped = wrap_with_proposer_quality(agent, ProposerQualityConfig(best_of_n=3))

    assert isinstance(wrapped, NativeSlateAdapter)
    chosen = await wrapped.propose(
        _context(workspace, round_event_emitter=lambda kind, fields: events.append((kind, fields)))
    )

    launches = _launches(records)
    assert len(launches) == 1
    assert len(launches[0]["prompts"]) == 4
    assert chosen.hypothesis.core_idea == "best"
    audited = next(fields for kind, fields in events if kind == "critique_selected")
    assert [item["core_idea"] for item in audited["slate"]] == ["small", "different", "best"]
    assert audited["index"] == 2
    assert audited["rationale"] == "best grounded change"


def test_native_slate_rejects_stage_model_overrides(agent: PiProposerAgent) -> None:
    with pytest.raises(ValueError, match="remove unsupported role override.*proposer_review"):
        wrap_with_proposer_quality(
            agent,
            ProposerQualityConfig(best_of_n=3),
            depth_model="separate-review-model",
        )


@pytest.mark.asyncio
async def test_a_retry_is_a_follow_up_on_one_process(
    agent: PiProposerAgent, workspace: Path, tmp_path: Path, records: Path
) -> None:
    _script(
        tmp_path,
        [{"emit": _experiment_args("not_in_the_manifest")}, {"emit": _experiment_args()}],
    )

    experiment = await agent.propose(_context(workspace))

    assert [p.mutation_id for p in experiment.patches] == ["router__sp"]
    launches = _launches(records)
    assert len(launches) == 1, "the retry restarted the process instead of reusing the session"
    assert len(launches[0]["prompts"]) == 2
    assert "not_in_the_manifest" in launches[0]["prompts"][1], "the repair turn lost its feedback"


@pytest.mark.asyncio
async def test_settling_without_the_tool_is_an_empty_response(
    agent: PiProposerAgent, workspace: Path, tmp_path: Path, records: Path
) -> None:
    _script(tmp_path, [{"emit": None}, {"emit": _experiment_args()}])

    experiment = await agent.propose(_context(workspace))

    assert isinstance(experiment, Experiment)
    assert len(_launches(records)[0]["prompts"]) == 2


@pytest.mark.asyncio
async def test_a_rejected_prompt_exhausts_the_budget(
    agent: PiProposerAgent, workspace: Path, tmp_path: Path
) -> None:
    _script(tmp_path, [{"reject": "the agent is busy"}] * 3)

    with pytest.raises(ProposerError, match="the agent is busy"):
        await agent.propose(_context(workspace))


@pytest.mark.asyncio
async def test_no_model_refuses_to_launch(
    agent: PiProposerAgent, workspace: Path, tmp_path: Path, records: Path
) -> None:
    _script(tmp_path, [{"emit": _experiment_args()}])

    with pytest.raises(ProposerError, match="no model on the ProposerContext"):
        await agent.propose(_context(workspace, model=""))

    assert _launches(records) == [], "a process was launched without a resolved model"


@pytest.mark.asyncio
async def test_a_missing_binary_is_a_proposer_error(
    workspace: Path, tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv("ZICATO_PI_STUB_SCRIPT", str(tmp_path / "script.json"))
    _script(tmp_path, [])
    config = ExternalProposerConfig(
        dotted_path="zicato.proposer.pi_agent:PiProposerAgent",
        workspace_root=workspace,
        options={"pi_bin": str(tmp_path / "no-such-pi")},
    )
    agent = PiProposerAgent(spec=ProposerSpec.default(), config=config)

    with pytest.raises(ProposerError, match="could not launch"):
        await agent.propose(_context(workspace))


@pytest.mark.asyncio
async def test_the_agent_dir_is_isolated_and_removed(
    agent: PiProposerAgent, workspace: Path, tmp_path: Path, records: Path
) -> None:
    _script(tmp_path, [{"emit": _experiment_args()}])

    await agent.propose(_context(workspace))

    launch = _launches(records)[0]
    agent_dir = Path(launch["env"]["PI_CODING_AGENT_DIR"])
    assert agent_dir.parent == workspace / ".pi-proposer"
    assert Path(launch["cwd"]) == agent_dir / "cwd"
    # It held a copy of the operator's credentials; it does not outlive the call.
    assert list((workspace / ".pi-proposer").iterdir()) == []


@pytest.mark.asyncio
async def test_concurrent_challengers_get_disjoint_dirs(
    agent: PiProposerAgent, workspace: Path, tmp_path: Path, records: Path
) -> None:
    import asyncio

    _script(tmp_path, [{"emit": _experiment_args()}] * 4)

    await asyncio.gather(*(agent.propose(_context(workspace)) for _ in range(3)))

    dirs = {launch["env"]["PI_CODING_AGENT_DIR"] for launch in _launches(records)}
    assert len(dirs) == 3
