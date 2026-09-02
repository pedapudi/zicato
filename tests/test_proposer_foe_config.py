"""The workspace `proposer` block: what it accepts and what it refuses.

The block is the only place a workspace decides how its proposal episodes
run, so a shape it gets wrong has to be named before a round opens rather
than at the first propose. These cases pin each refusal's wording, and
pin that a workspace still configured for a retired proposer runtime is
told what replaced it instead of quietly running Foe.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zicato.proposer.external import DEFAULT_PROPOSER_AGENT
from zicato.proposer.foe_config import (
    FoeBudget,
    ProposerConfigError,
    load_foe_proposer_config,
    refuse_removed_proposer_directory,
)


def _config(**overrides: object) -> dict[str, object]:
    block: dict[str, object] = {
        "binary": "/usr/local/bin/foe",
        "model": {"provider": "example", "model": "example-model"},
    }
    block.update(overrides)
    return {"proposer": block}


def test_a_minimal_block_takes_the_documented_defaults() -> None:
    config = load_foe_proposer_config(_config())
    assert config.binary == Path("/usr/local/bin/foe")
    assert config.model.provider == "example"
    assert config.viewer == "off"
    assert config.budget == FoeBudget()


def test_every_declared_dimension_is_read_off_the_budget() -> None:
    config = load_foe_proposer_config(
        _config(
            budget={
                "model_calls": 20,
                "seconds": 1200,
                "input_tokens": 500_000,
                "output_tokens": 80_000,
            }
        )
    )
    assert config.budget == FoeBudget(
        model_calls=20, seconds=1200, input_tokens=500_000, output_tokens=80_000
    )


def test_an_omitted_seconds_key_keeps_the_deadline_and_a_null_removes_it() -> None:
    assert load_foe_proposer_config(_config(budget={"model_calls": 4})).budget.seconds == 900
    unbounded = load_foe_proposer_config(_config(budget={"model_calls": 4, "seconds": None}))
    assert unbounded.budget.seconds is None


def test_a_credential_travels_as_a_file_named_in_the_model_options() -> None:
    config = load_foe_proposer_config(
        _config(
            model={
                "provider": "example",
                "model": "example-model",
                "options": {"api_key_file": "/home/me/.config/foe/key.json"},
            }
        )
    )
    assert config.model.options == {"api_key_file": "/home/me/.config/foe/key.json"}


def test_a_workspace_with_no_proposer_block_is_refused() -> None:
    with pytest.raises(ProposerConfigError, match="declares no `proposer` block"):
        load_foe_proposer_config({})


def test_a_relative_binary_path_is_refused() -> None:
    with pytest.raises(ProposerConfigError, match="must be an absolute path"):
        load_foe_proposer_config(_config(binary="foe"))


def test_a_viewer_policy_outside_the_closed_set_is_refused() -> None:
    with pytest.raises(ProposerConfigError, match="proposer.viewer must be one of"):
        load_foe_proposer_config(_config(viewer="sometimes"))


def test_a_budget_below_one_model_call_is_refused() -> None:
    with pytest.raises(ProposerConfigError, match="model_calls must be >= 1"):
        load_foe_proposer_config(_config(budget={"model_calls": 0}))


def test_a_model_block_with_no_provider_is_refused() -> None:
    with pytest.raises(ProposerConfigError, match="proposer.model.provider"):
        load_foe_proposer_config(_config(model={"model": "example-model"}))


@pytest.mark.parametrize("key", ["pi_bin", "pi_integration_dir"])
def test_a_retired_runtime_key_is_refused_by_name(key: str) -> None:
    workspace = _config()
    workspace["runtime"] = {key: "/opt/pi/bin/pi"}
    with pytest.raises(ProposerConfigError) as raised:
        load_foe_proposer_config(workspace)
    assert key in str(raised.value)
    assert "was removed" in str(raised.value)
    assert "docs/design/PROPOSER.md" in str(raised.value)


@pytest.mark.parametrize(
    "dotted",
    [
        "zicato.proposer.retired_runtime:RetiredProposerAgent",
        "zicato.proposer.something:SomethingElse",
    ],
)
def test_a_built_in_proposer_class_other_than_foe_is_refused(dotted: str) -> None:
    """Every built-in runtime but Foe was removed, so the namespace is closed."""
    workspace = _config()
    workspace["runtime"] = {"proposer_agent": dotted}
    with pytest.raises(ProposerConfigError, match="was removed"):
        load_foe_proposer_config(workspace)


def test_the_foe_agent_is_the_one_class_the_namespace_still_admits() -> None:
    workspace = _config()
    workspace["runtime"] = {"proposer_agent": DEFAULT_PROPOSER_AGENT}
    assert load_foe_proposer_config(workspace).viewer == "off"


def test_an_operator_supplied_proposer_class_is_still_accepted() -> None:
    workspace = _config()
    workspace["runtime"] = {"proposer_agent": "acme.proposers:HouseProposer"}
    assert load_foe_proposer_config(workspace).binary == Path("/usr/local/bin/foe")


def test_a_proposer_directory_carrying_an_agent_module_is_refused(tmp_path: Path) -> None:
    directory = tmp_path / "proposers" / "house"
    (directory / "skills").mkdir(parents=True)
    (directory / "agent.py").write_text("agent = None\n", encoding="utf-8")
    with pytest.raises(ProposerConfigError, match="custom proposer agent modules were removed"):
        refuse_removed_proposer_directory(directory)


def test_a_proposer_directory_of_skills_alone_is_accepted(tmp_path: Path) -> None:
    directory = tmp_path / "proposers" / "house"
    (directory / "skills").mkdir(parents=True)
    (directory / "skills" / "grounding.md").write_text("Ground it.\n", encoding="utf-8")
    refuse_removed_proposer_directory(directory)
