"""The proposer's contract identity is the runtime's, plus zicato's inputs.

The contract hash decides when an epoch rolls, so it must move for
everything the model sees and for nothing else. Reconstructing the
proposer's surface from outside cannot see the strings the runtime itself
contributes; asking the runtime for its own fingerprint closes that gap.

These cases pin the rule in both directions — a reworded tool description
moves the hash, a moved grant path does not — and pin that the identity is
computable on a machine that could never run a round, because it starts no
process of its own and reads no credential.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import foe
import pytest

from tests._foe_support import fake_foe_binary
from zicato.epoch.contract import _canon_proposer
from zicato.proposer.external import external_proposer_config
from zicato.proposer.foe_agent import FoeProposerAgent, build_episode_tools
from zicato.proposer.foe_config import ProposerConfigError, load_foe_proposer_config
from zicato.proposer.foe_request import identity_contract
from zicato.proposer.skills import resolve_proposer_spec


def _workspace(tmp_path: Path, **proposer: object) -> dict[str, object]:
    block: dict[str, object] = {
        "binary": str(fake_foe_binary(tmp_path / "bin")),
        "model": {"provider": "exec", "model": "scripted"},
    }
    block.update(proposer)
    return {"proposer": block}


def _identity(tmp_path: Path, config: dict[str, object]) -> dict[str, object]:
    binding = external_proposer_config(config, tmp_path)
    assert binding is not None
    return dict(FoeProposerAgent.contract_identity(binding))


def _tools(tmp_path: Path) -> tuple[foe.HostTool, ...]:
    return build_episode_tools(
        workspace_root=tmp_path,
        generation_root=tmp_path / "read",
        scratch_root=tmp_path / "write",
        epoch_id="",
        generation_id="",
        mutations=(),
    ).as_sequence()


def test_the_identity_is_foe_s_fingerprint_beside_zicato_s_inputs(tmp_path: Path) -> None:
    identity = _identity(tmp_path, _workspace(tmp_path))
    assert identity["kind"] == "foe"
    assert str(identity["contract_fingerprint"]).startswith("sha256:")
    assert "validate_patches" in identity["tools"]  # type: ignore[operator]


def test_rewording_a_tool_description_moves_the_hash(tmp_path: Path) -> None:
    """What the model reads is what rolls the epoch."""
    config = load_foe_proposer_config(_workspace(tmp_path), tmp_path)
    usage, verify = _tools(tmp_path)
    before = identity_contract(config, (usage, verify)).fingerprint(config.binary)

    # The same tool with the same schema and one more sentence of
    # description, so the description is the only thing that could move.
    reworded = foe.tool(
        name=verify.spec.name, description=verify.spec.description + " Call it twice."
    )(verify.fn)
    assert reworded.spec.params == verify.spec.params

    after = identity_contract(config, (usage, reworded)).fingerprint(config.binary)
    assert after != before


def test_changing_a_grant_path_leaves_the_hash_alone(tmp_path: Path) -> None:
    """Where the episode runs is not what the model reads."""
    config = load_foe_proposer_config(_workspace(tmp_path), tmp_path)
    contract = identity_contract(config, _tools(tmp_path))
    before = contract.fingerprint(config.binary)

    moved = tmp_path / "elsewhere"
    contract.grants = foe.Grants(read=[moved / "read"], write=[moved / "write"])
    assert contract.fingerprint(config.binary) == before


def test_raising_the_budget_moves_the_hash(tmp_path: Path) -> None:
    """A proposer with more model calls investigates differently."""
    base = _identity(tmp_path, _workspace(tmp_path))
    raised = _identity(tmp_path, _workspace(tmp_path, budget={"model_calls": 40}))
    assert raised["contract_fingerprint"] != base["contract_fingerprint"]


def test_selecting_another_model_leaves_the_hash_alone(tmp_path: Path) -> None:
    """Model selection is runtime infrastructure and rolls no epoch."""
    base = _identity(tmp_path, _workspace(tmp_path))
    other = _identity(
        tmp_path,
        _workspace(tmp_path, model={"provider": "example", "model": "some-other-model"}),
    )
    assert other["contract_fingerprint"] == base["contract_fingerprint"]


#: What a credential-carrying environment variable is called, whatever
#: names it. Anything whose name says key, token, secret or credential is
#: deleted before the identity is computed.
_CREDENTIAL_SHAPED = re.compile(r"(API_KEY|_TOKEN|_SECRET|CREDENTIAL)", re.IGNORECASE)


def test_the_identity_is_computed_with_no_credential_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Asserted by running with nowhere for a credential to be found.

    The credential search is emptied from both directions: the config
    home points at an empty directory, and every environment variable
    whose NAME is credential-shaped is deleted. Matching on shape rather
    than on a list of names is what makes the claim total — a list can
    forget a provider, and naming providers is not this repository's
    business.
    """
    empty_home = tmp_path / "no-home"
    empty_home.mkdir()
    monkeypatch.setenv("HOME", str(empty_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(empty_home / ".config"))
    for key in [k for k in os.environ if _CREDENTIAL_SHAPED.search(k)]:
        monkeypatch.delenv(key, raising=False)
    assert str(_identity(tmp_path, _workspace(tmp_path))["contract_fingerprint"]).startswith(
        "sha256:"
    )


def test_a_binary_that_cannot_report_its_identity_refuses_the_freeze(tmp_path: Path) -> None:
    config = _workspace(tmp_path)
    config["proposer"]["binary"] = str(tmp_path / "bin" / "absent-foe")  # type: ignore[index]
    with pytest.raises(ProposerConfigError, match="could not report the proposer's"):
        _identity(tmp_path, config)


def test_the_contract_canon_folds_the_runtime_identity(tmp_path: Path) -> None:
    """The canonical proposer component carries what Foe reported."""
    binding = external_proposer_config(_workspace(tmp_path), tmp_path)
    canon = json.loads(_canon_proposer(None, binding))
    assert canon["agent_id"] == "external:foe"
    assert canon["external"]["path"] == "zicato.proposer.foe_agent:FoeProposerAgent"
    assert len(canon["external"]["identity_sha256"]) == 64


def test_the_canon_moves_when_the_runtime_identity_does(tmp_path: Path) -> None:
    base = external_proposer_config(_workspace(tmp_path), tmp_path)
    wider = external_proposer_config(_workspace(tmp_path, budget={"model_calls": 40}), tmp_path)
    assert _canon_proposer(None, base) != _canon_proposer(None, wider)


def test_a_workspace_that_declares_no_proposer_resolves_none(tmp_path: Path) -> None:
    """An undeclared proposer still hashes; a round is what refuses it."""
    assert external_proposer_config({}, tmp_path) is None
    spec = resolve_proposer_spec(None, None)
    assert spec.external_path is None
