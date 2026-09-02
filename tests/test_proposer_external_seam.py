"""The external-proposer seam: an operator's own ``runtime.proposer_agent``.

The seam is the loop's single proposer boundary. Its default and only
supported implementation is the Foe-backed proposer; what these cases
cover is the other door — a class the operator supplies themselves.

* **resolution** — a dotted path becomes an agent, with an error that
  names the field when it does not;
* **identity** — the class's causal surface is folded into the proposer
  contract component, so configuring an external proposer (or changing
  what it runs on) rolls the epoch;
* **silence when unconfigured** — the canonical proposer form and the
  contract hash of a workspace that names no external proposer do not
  move. That is the pin: this seam must be invisible to every existing
  workspace.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from zicato.core.types import Experiment, ProposerSpec
from zicato.epoch.contract import (
    ContractInputs,
    _canon_proposer,
    compute_component_hashes,
    compute_contract_hash,
)
from zicato.proposer.agent import ProposerContext, build_proposer_agent
from zicato.proposer.external import (
    ExternalProposerConfig,
    external_proposer_config,
    load_external_proposer_class,
    resolve_external_spec,
)
from zicato.proposer.skills import resolve_proposer_spec

#: The canonical form a workspace that declares no proposal runtime
#: hashes to, spelled out. This literal is the guard: any change to
#: `_canon_proposer`'s shape that reaches such a workspace moves every
#: epoch in every workspace, including the ones that can still hash their
#: contract on a machine with no Foe binary.
BUILTIN_CANON = '{"agent_id": "builtin:default", "skills": [], "tools": []}'

_DOTTED = "tests.test_proposer_external_seam:StubExternalAgent"


class StubExternalAgent:
    """A minimal external proposer — the seam without a subprocess."""

    external_id = "stub"

    def __init__(self, *, spec: ProposerSpec, config: ExternalProposerConfig) -> None:
        self.spec = spec
        self.config = config

    @classmethod
    def contract_identity(cls, config: ExternalProposerConfig) -> Mapping[str, Any]:
        return {
            "kind": "stub",
            "tools": ["propose_experiment"],
            "knob": config.options.get("stub_knob", ""),
        }

    async def propose(self, ctx: ProposerContext) -> Experiment:  # pragma: no cover - unused
        raise NotImplementedError


class NotAnAgent:
    """A class without ``contract_identity`` — the misconfiguration case."""


def _config(**options: str) -> ExternalProposerConfig:
    return ExternalProposerConfig(dotted_path=_DOTTED, options=options)


def _inputs(tmp_path: Path, external: ExternalProposerConfig | None = None) -> ContractInputs:
    (tmp_path / "board.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "brief.md").write_text("# brief\n", encoding="utf-8")
    (tmp_path / "scoring.json").write_text("{}", encoding="utf-8")
    return ContractInputs(
        board_path=tmp_path / "board.jsonl",
        brief_path=tmp_path / "brief.md",
        scoring_path=tmp_path / "scoring.json",
        entrypoint="pkg.mod:agent",
        mutable_trees=("src",),
        external_proposer=external,
    )


# -- resolution --------------------------------------------------------------


def test_config_is_none_without_the_key() -> None:
    assert external_proposer_config({}) is None
    assert external_proposer_config({"runtime": {"auxiliary_call_llm": "pkg:fn"}}) is None


def test_config_reads_the_runtime_block() -> None:
    config = external_proposer_config(
        {"runtime": {"proposer_agent": _DOTTED, "instance_id": "ws-1", "parallelism": 4}},
        Path("/ws"),
    )
    assert config is not None
    assert config.dotted_path == _DOTTED
    assert config.workspace_root == Path("/ws")
    # Non-string runtime values are dropped rather than coerced.
    assert config.options == {"proposer_agent": _DOTTED, "instance_id": "ws-1"}


def test_build_proposer_agent_resolves_the_external_class_first() -> None:
    config = _config()
    spec = resolve_external_spec(config)
    agent = build_proposer_agent(spec, external_config=config)
    assert isinstance(agent, StubExternalAgent)
    assert agent.spec is spec


def test_build_proposer_agent_requires_the_config() -> None:
    spec = resolve_external_spec(_config())
    with pytest.raises(ValueError, match="no external_config was supplied"):
        build_proposer_agent(spec)


@pytest.mark.parametrize(
    ("dotted", "match"),
    [
        ("tests.test_proposer_external_seam:Missing", "has no attribute 'Missing'"),
        ("no.such.module:Thing", "could not import module"),
        ("tests.test_proposer_external_seam:NotAnAgent", "contract_identity"),
    ],
)
def test_bad_paths_name_the_field_that_produced_them(dotted: str, match: str) -> None:
    with pytest.raises(ValueError, match=match) as excinfo:
        load_external_proposer_class(dotted)
    assert "runtime.proposer_agent" in str(excinfo.value)


# -- identity ----------------------------------------------------------------


def test_spec_carries_the_external_identity() -> None:
    spec = resolve_external_spec(_config())
    assert spec.agent_id == "external:stub"
    assert spec.external_path == _DOTTED
    assert spec.external_identity_sha256 is not None
    # The spec's tools come off the SAME mapping that is hashed, so the
    # sanctioned set and the hashed set cannot drift apart.
    assert spec.tools == ("propose_experiment",)


def test_a_changed_causal_surface_rolls_the_identity() -> None:
    before = resolve_external_spec(_config(stub_knob="a")).external_identity_sha256
    after = resolve_external_spec(_config(stub_knob="b")).external_identity_sha256
    assert before != after


def test_skills_survive_alongside_an_external_agent(tmp_path: Path) -> None:
    """A proposer dir may steer an external agent; those are the HASHED skills."""
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "focus.md").write_text("Change the lever, not the wording.\n", encoding="utf-8")

    spec = resolve_proposer_spec(tmp_path, _config())

    assert spec.agent_id == "external:stub"
    assert [s.name for s in spec.skills] == ["focus"]


# -- contract folding --------------------------------------------------------


def test_a_workspace_declaring_no_runtime_canonicalizes_without_one() -> None:
    """It hashes, and it hashes without asking any binary anything."""
    assert _canon_proposer(None) == BUILTIN_CANON
    assert json.loads(_canon_proposer(None)) == json.loads(BUILTIN_CANON)


def test_unconfigured_contract_hash_does_not_move(tmp_path: Path) -> None:
    """The pin: naming no external proposer hashes exactly as before."""
    assert compute_contract_hash(_inputs(tmp_path)) == compute_contract_hash(
        _inputs(tmp_path, None)
    )


def test_configuring_an_external_proposer_rolls_the_epoch(tmp_path: Path) -> None:
    plain = compute_component_hashes(_inputs(tmp_path))
    external = compute_component_hashes(_inputs(tmp_path, _config()))

    assert plain["proposer"] != external["proposer"]
    # ...and nothing else in the contract moved with it.
    assert {k: v for k, v in plain.items() if k != "proposer"} == {
        k: v for k, v in external.items() if k != "proposer"
    }


def test_the_canon_carries_the_path_and_the_digest(tmp_path: Path) -> None:
    canon = json.loads(_canon_proposer(None, _config()))
    spec = resolve_external_spec(_config())

    assert canon["external"] == {
        "path": _DOTTED,
        "identity_sha256": spec.external_identity_sha256,
    }
