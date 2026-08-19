"""Every registered contract component survives epoch creation (issue #186).

An epoch freezes the evaluation contract. Three of its components are
files the epoch copies into ``epochs/{id}/`` (board, proposer brief,
scoring); the rest are *registered* in ``config.json`` and are carried
into the epoch's contract hash by whoever creates the epoch — the
``zicato epoch new`` CLI (hand-forced boundary) and
``zicato.evolve.epoching._create_epoch_from_contract`` (auto-roll).

Historically each creator re-derived those registered components by
hand, one keyword argument at a time, and the enumeration drifted from
:class:`~zicato.epoch.contract.ContractInputs`: ``epoch new`` read only
``adk_entrypoint`` and ``mutable_trees``, so a workspace with a
registered proposer dir froze ``proposer_path=None`` — silently swapping
``dir:<name>`` for the built-in default proposer for the whole epoch.
Both creators also dropped ``external_proposer`` and
``proposer_static_checks``, so a workspace configuring either one rolled
its epoch on every ``evolve``: the stored hash could never equal the
hash the orchestrator recomputes from the live contract.

The invariant pinned here is one sentence: **an epoch's stored contract
hash equals the hash of the live contract it was created from**, for
both creators. :func:`test_every_contract_component_is_exercised` is the
completeness guard that keeps it honest — it fails when a field is added
to ``ContractInputs`` without the fixture below configuring it, so the
next component cannot be dropped in silence.
"""

from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path

import pytest
from click.testing import CliRunner

from zicato.cli.commands.epoch import epoch_grp
from zicato.epoch.contract import (
    ContractInputs,
    compute_contract_hash,
    resolve_contract_inputs,
)
from zicato.epoch.lifecycle import current_epoch_id, load_epoch

#: An external proposer class that already exists for the seam tests —
#: reused here so this fixture does not grow a second stub.
_EXTERNAL_AGENT = "tests.test_proposer_external_seam:StubExternalAgent"

_BOARD = '{"id": "e1", "kind": "single_turn", "wall_clock_budget_seconds": 60, "input": "hi"}\n'
_BRIEF = "# Proposer brief\n\nMake it faster.\n"
_SCORING = '{"pass_weight": 2.0}\n'


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """A workspace whose ``config.json`` registers EVERY contract component.

    Every field of :class:`ContractInputs` is set to a non-default value
    here, which is what makes the hash-equality assertions below able to
    catch a dropped component: with the defaults in place a drop is
    indistinguishable from a correct carryover.
    """
    (tmp_path / "board.jsonl").write_text(_BOARD, encoding="utf-8")
    (tmp_path / "brief.md").write_text(_BRIEF, encoding="utf-8")
    (tmp_path / "scoring.json").write_text(_SCORING, encoding="utf-8")

    proposer = tmp_path / "proposers" / "tuned"
    proposer.mkdir(parents=True)
    (proposer / "agent.json").write_text(
        json.dumps({"agent_id": "dir:tuned", "tools": ["read_file"]}),
        encoding="utf-8",
    )

    ws = tmp_path / ".zicato"
    ws.mkdir()
    (ws / "config.json").write_text(
        json.dumps(
            {
                "adk_entrypoint": "pkg.mod:agent",
                "mutable_trees": ["src/agent"],
                "contract": {
                    "board_path": str(tmp_path / "board.jsonl"),
                    "brief_path": str(tmp_path / "brief.md"),
                    "scoring_path": str(tmp_path / "scoring.json"),
                    "proposer_path": "proposers/tuned",
                    "proposer_static_checks": ["ruff", "mypy"],
                },
                "runtime": {"proposer_agent": _EXTERNAL_AGENT},
            }
        ),
        encoding="utf-8",
    )
    return ws


def test_every_contract_component_is_exercised(workspace: Path) -> None:
    """THE COMPLETENESS GUARD: no contract component may sit at its default.

    A carryover bug is only observable when the dropped component would
    have changed the hash, i.e. when it is configured. So this guard
    walks ``fields(ContractInputs)`` and requires the fixture above to
    have configured each one. Add a field to ``ContractInputs`` and this
    test fails until the fixture registers it — at which point the
    hash-equality tests below cover it automatically.
    """
    resolved = resolve_contract_inputs(workspace)
    unexercised = [
        f.name
        for f in fields(ContractInputs)
        if not getattr(resolved, f.name)  # empty string / empty tuple / None
    ]
    assert not unexercised, (
        f"contract component(s) left at their default by the fixture: {sorted(unexercised)}. "
        "Register them in the `workspace` fixture's config.json — an unconfigured "
        "component cannot reveal a carryover bug, so leaving one unset silently "
        "narrows every hash-equality test in this module."
    )


def test_hand_forced_epoch_carries_the_registered_contract(workspace: Path) -> None:
    """``zicato epoch new`` freezes the hash the live contract resolves to.

    The issue's headline symptom: without the registered ``proposer_path``
    the epoch hashes under the built-in-proposer canon, so the very first
    ``evolve`` sees drift and rolls the epoch the operator just forced.
    """
    project = workspace.parent
    result = CliRunner().invoke(
        epoch_grp,
        [
            "new",
            "hand-forced",
            "--workspace",
            str(workspace),
            "--board",
            str(project / "board.jsonl"),
            "--brief",
            str(project / "brief.md"),
            "--scoring",
            str(project / "scoring.json"),
            "--goal",
            "",
        ],
    )
    assert result.exit_code == 0, result.output

    epoch_id = current_epoch_id(workspace)
    assert epoch_id is not None
    cfg = load_epoch(workspace, epoch_id)
    assert cfg.contract_hash == compute_contract_hash(resolve_contract_inputs(workspace))
    # The proposer is also read back off the epoch directly — that field is
    # what `build_proposer_agent` runs the epoch's rounds with.
    assert cfg.proposer_path == (project / "proposers" / "tuned").resolve()


def test_mixing_contract_with_the_shorthand_is_rejected(workspace: Path) -> None:
    """Two spellings of one component must not resolve by precedence.

    Rejecting the combination before anything is written is what keeps
    the shorthand from re-becoming a place a component can go missing.
    """
    from zicato.core.types import ScoringWeights
    from zicato.epoch.lifecycle import new_epoch

    project = workspace.parent
    with pytest.raises(ValueError, match="not both"):
        new_epoch(
            workspace,
            "mixed",
            project / "board.jsonl",
            project / "brief.md",
            ScoringWeights(),
            contract=resolve_contract_inputs(workspace),
            entrypoint="pkg.other:agent",
        )
    assert current_epoch_id(workspace) is None


def test_auto_rolled_epoch_carries_the_registered_contract(workspace: Path) -> None:
    """The auto-roll creator freezes the hash it was handed, component for component.

    ``ensure_epoch_for_contract`` compares the stored hash against a hash
    computed from the full resolved inputs, so any component the creator
    drops makes the two disagree forever — a fresh epoch on every round.
    """
    from zicato.evolve.epoching import _create_epoch_from_contract

    inputs = resolve_contract_inputs(workspace)
    epoch_id = _create_epoch_from_contract(workspace, inputs=inputs, name="e0", aux_call_llm=None)
    cfg = load_epoch(workspace, epoch_id)
    assert cfg.contract_hash == compute_contract_hash(inputs)
