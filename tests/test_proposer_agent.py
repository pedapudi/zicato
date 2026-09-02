"""What :func:`build_proposer_agent` resolves, and what it refuses.

The builder is the one place a resolved
:class:`~zicato.core.types.ProposerSpec` becomes something callable, and
there is one supported answer: the Foe-backed agent, reached because the
workspace declared a ``proposer`` block. These pin that answer, the
operator's own class as the one other thing the seam accepts, and the two
refusals that stand between them and a silent default — a workspace that
declared no proposal runtime at all, and a proposer directory still
carrying an executable agent module.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests._foe_support import stand_in_proposer_block
from zicato.core.types import Experiment, ProposerSkill, ProposerSpec
from zicato.proposer.agent import ProposerContext, build_proposer_agent
from zicato.proposer.external import external_proposer_config, resolve_external_spec
from zicato.proposer.foe_agent import FoeProposerAgent
from zicato.proposer.foe_config import ProposerConfigError


class OperatorAgent:
    """An operator's own implementation of the retained protocol."""

    external_id = "operator"

    def __init__(self, *, spec: ProposerSpec, config: Any) -> None:
        self.spec = spec
        self.config = config

    @classmethod
    def contract_identity(cls, config: Any) -> dict[str, Any]:
        del config
        return {"kind": "operator", "tools": ["edit"]}

    async def propose(self, ctx: ProposerContext) -> Experiment:  # pragma: no cover
        raise AssertionError("this stand-in is resolved, never run")


_OPERATOR_PATH = "tests.test_proposer_agent:OperatorAgent"


def _workspace_config(tmp_path: Path, **runtime: str) -> dict[str, Any]:
    config: dict[str, Any] = {"proposer": stand_in_proposer_block(tmp_path / "foe")}
    if runtime:
        config["runtime"] = dict(runtime)
    return config


def _resolve(tmp_path: Path, **runtime: str) -> Any:
    """Resolve a workspace the way a round does: one reading, one binding."""
    binding = external_proposer_config(_workspace_config(tmp_path, **runtime), tmp_path)
    assert binding is not None
    return build_proposer_agent(resolve_external_spec(binding), external_config=binding)


def test_a_declared_proposer_block_resolves_the_foe_agent(tmp_path: Path) -> None:
    agent = _resolve(tmp_path)
    assert isinstance(agent, FoeProposerAgent)
    assert agent.spec.agent_id == "external:foe"


def test_an_operator_class_wins_over_the_default(tmp_path: Path) -> None:
    """The seam accepts an explicit class of the operator's own."""
    agent = _resolve(tmp_path, proposer_agent=_OPERATOR_PATH)
    assert isinstance(agent, OperatorAgent)
    assert agent.spec.agent_id == "external:operator"
    assert agent.spec.external_path == _OPERATOR_PATH


def test_the_spec_carries_the_proposer_dir_s_skills(tmp_path: Path) -> None:
    """A dir's skills steer the agent whatever implements the protocol."""
    binding = external_proposer_config(_workspace_config(tmp_path), tmp_path)
    assert binding is not None
    skill = ProposerSkill(name="diversify", description="change the lever", body="body")
    spec = resolve_external_spec(binding, skills=(skill,))
    assert spec.skills == (skill,)


def test_a_workspace_that_declared_no_runtime_is_refused() -> None:
    """No block, no bound class: the builder names the block to write."""
    spec = ProposerSpec.default()
    with pytest.raises(ValueError, match="names no proposal runtime"):
        build_proposer_agent(spec)


def test_a_named_agent_without_its_configuration_is_refused() -> None:
    spec = ProposerSpec(agent_id="external:foe", tools=(), skills=(), external_path=_OPERATOR_PATH)
    with pytest.raises(ValueError, match="no external_config"):
        build_proposer_agent(spec)


def test_a_proposer_dir_carrying_an_agent_module_is_refused(tmp_path: Path) -> None:
    """A removed runtime's configuration is named rather than ignored."""
    proposer_dir = tmp_path / "proposers" / "demo"
    proposer_dir.mkdir(parents=True)
    (proposer_dir / "agent.py").write_text("agent = object()\n", encoding="utf-8")
    binding = external_proposer_config(_workspace_config(tmp_path), tmp_path)
    assert binding is not None

    with pytest.raises(ProposerConfigError, match="custom proposer agent modules were removed"):
        build_proposer_agent(
            resolve_external_spec(binding),
            proposer_path=proposer_dir,
            external_config=binding,
        )
