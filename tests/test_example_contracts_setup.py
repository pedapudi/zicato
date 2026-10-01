"""Every shipped example contract passes the gate ``evolve`` runs before spending.

Each case wires a fresh workspace the way the example's ``RUN.md`` does:
``zicato init``, ``zicato epoch register``, the model engines and the
test suite's stand-in proposal runtime in ``config.json``, and the
board, brief and scoring files copied unedited to the canonical location
next to the workspace. The case then runs
:func:`zicato.check.require_workspace_valid` with ``live_contract=True``,
which is the check ``zicato evolve`` and ``zicato inspect setup`` run
before a round spends anything. The check imports the harness and loads
one snapshot in a subprocess; it calls no model and runs no board entry.

A contract that ships without a block its adapter requires (the ``goldfive``
object in ``scoring.json`` for the agent-kit adapter, for instance) fails
here instead of in an operator's first ``evolve``.

Each case also checks that every mutation id the brief lists under
``Preferred edits`` or ``Forbidden edits`` names a point on the registered
surface, so a brief cannot steer the proposer at an id that does not
exist, and that no preferred id is a numeric manifest point. The manifest
bridge records a numeric point's content from the manifest rather than
from the source, so a proposal cannot change one: the read-back refuses
the edited file as an edit outside every mutation point.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from click.testing import CliRunner

import zicato_examples
from tests._foe_support import stand_in_proposer_block
from zicato.check import require_workspace_valid
from zicato.cli.discovery import build_cli_root
from zicato.mutation.enumerator import enumerate_mutations
from zicato.proposer.brief import parse_brief

EXAMPLES = Path(zicato_examples.__file__).resolve().parent

#: The brief ``target_0_convergence/RUN.md`` writes, because that example
#: ships no ``brief.md`` of its own.
_CONVERGENCE_BRIEF = (
    "# Convergence brief\n"
    "- Remove defect tokens from the writing policy, one per round.\n"
    "- Never fabricate metrics.\n"
)


def _goldfive_package() -> Path:
    goldfive = pytest.importorskip("goldfive")
    return Path(goldfive.__file__).resolve().parent


def _engines(mocks: str) -> dict[str, object]:
    return {
        "engines": {
            "target": {"call_llm": f"{mocks}:target_llm"},
            "evaluation": {"call_llm": f"{mocks}:aux_llm"},
        },
        "roles": {},
    }


@dataclass(frozen=True)
class ExampleContract:
    """One example contract, wired as its ``RUN.md`` wires it."""

    example: str
    scoring: str
    #: ``epoch register`` arguments naming the harness; the mutable tree
    #: comes from :attr:`mutable_tree`.
    harness: tuple[str, ...]
    #: The registered mutable tree, resolved when the case runs.
    mutable_tree: Callable[[], Path]
    #: The brief file inside the example directory, or ``None`` when the
    #: case writes :data:`_CONVERGENCE_BRIEF`.
    brief: str | None
    #: The dotted module whose ``target_llm`` and ``aux_llm`` the engines
    #: name, or ``None`` when the example declares no engines.
    mocks: str | None
    #: A proposer directory recorded as a contract input, relative to the
    #: example directory.
    proposer_dir: str | None = None

    @property
    def directory(self) -> Path:
        return EXAMPLES / self.example


def _presentation(scoring: str) -> ExampleContract:
    return ExampleContract(
        example="target_1_presentation",
        scoring=scoring,
        harness=("--adk", "agent.agent:root_agent"),
        mutable_tree=lambda: EXAMPLES / "target_1_presentation" / "agent",
        brief="rubric.md",
        mocks="zicato_examples.target_1_presentation.mocks",
    )


def _convergence(scoring: str) -> ExampleContract:
    return ExampleContract(
        example="target_0_convergence",
        scoring=scoring,
        harness=("--factory", "zicato_examples.target_0_convergence.harness:make_adapter"),
        mutable_tree=lambda: EXAMPLES / "target_0_convergence" / "agent",
        brief=None,
        mocks="zicato_examples.target_0_convergence.mocks",
        proposer_dir="proposer",
    )


CONTRACTS: tuple[ExampleContract, ...] = (
    _convergence("scoring.json"),
    _convergence("scoring.effective.json"),
    _presentation("scoring.json"),
    _presentation("scoring.racing.json"),
    _presentation("scoring.single_elim.json"),
    _presentation("scoring.double_elim.json"),
    _presentation("scoring.swiss.json"),
    ExampleContract(
        example="target_2_goldfive_steering",
        scoring="scoring.json",
        harness=("--adk", "zicato_examples.target_2_goldfive_steering.agent_under_test:agent"),
        mutable_tree=_goldfive_package,
        brief="rubric.md",
        mocks="zicato_examples.target_2_goldfive_steering.mocks",
    ),
    ExampleContract(
        example="target_4_agent_config",
        scoring="scoring.json",
        harness=("--factory", "zicato_examples.target_4_agent_config.driver:make_adapter"),
        mutable_tree=lambda: EXAMPLES / "target_4_agent_config" / "config_package",
        brief="brief.md",
        mocks=None,
    ),
)


def test_every_shipped_scoring_file_is_covered() -> None:
    shipped = {(path.parent.name, path.name) for path in EXAMPLES.glob("target_*/scoring*.json")}
    covered = {(contract.example, contract.scoring) for contract in CONTRACTS}
    assert shipped == covered


def _wire_workspace(root: Path, contract: ExampleContract) -> tuple[Path, Path]:
    """Build the workspace ``RUN.md`` describes; return it and its mutable tree."""

    workspace = root / ".zicato"
    tree = contract.mutable_tree()
    runner = CliRunner()
    root_command = build_cli_root()
    for arguments in (
        ["init", "--workspace", str(workspace)],
        [
            "epoch",
            "register",
            "--workspace",
            str(workspace),
            *contract.harness,
            "--mutable-tree",
            str(tree),
        ],
    ):
        result = runner.invoke(root_command, arguments, catch_exceptions=False)
        assert result.exit_code == 0, result.output

    config_path = workspace / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if contract.mocks is not None:
        config["models"] = _engines(contract.mocks)
    config["proposer"] = stand_in_proposer_block(root / "foe")
    if contract.proposer_dir is not None:
        config["contract"] = {
            **(config.get("contract") or {}),
            "proposer_path": str(contract.directory / contract.proposer_dir),
        }
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    shutil.copyfile(contract.directory / "board.jsonl", root / "board.jsonl")
    shutil.copyfile(contract.directory / contract.scoring, root / "scoring.json")
    if contract.brief is None:
        (root / "brief.md").write_text(_CONVERGENCE_BRIEF, encoding="utf-8")
    else:
        shutil.copyfile(contract.directory / contract.brief, root / "brief.md")
    return workspace, tree


@pytest.mark.parametrize(
    "contract",
    CONTRACTS,
    ids=[f"{contract.example}/{contract.scoring}" for contract in CONTRACTS],
)
def test_example_contract_passes_the_pre_spend_gate(
    contract: ExampleContract, tmp_path: Path
) -> None:
    workspace, tree = _wire_workspace(tmp_path, contract)

    # Raises WorkspaceCheckError, whose message lists every blocking finding,
    # when the contract would be refused.
    require_workspace_valid(workspace, live_contract=True)

    brief = parse_brief((tmp_path / "brief.md").read_text(encoding="utf-8"))
    surface = {point.id: point for point in enumerate_mutations([tree])}
    named = set(brief.preferred_ids) | set(brief.forbidden_ids)
    assert named <= surface.keys(), sorted(named - surface.keys())
    unappliable = sorted(
        mutation_id
        for mutation_id in brief.preferred_ids
        if surface[mutation_id].metadata.get("manifest_kind") == "numeric"
    )
    assert not unappliable, unappliable
