"""``zicato init --example``: the seven artifacts, and a round that runs.

The claim the scaffold makes is that a new operator gets from an empty
directory to a settled round without authoring a file. These cases hold
it to that in two steps: what the scaffold puts on disk and wires into
``config.json``, and then the loop itself — a real round, through the
real proposer seam, the real applier, and the real subprocess tournament
workers, with no model, no endpoint, and no monkeypatch on the path under
test.

The scalars are exact. The board has four entries; three of the four
seeded style rules each fail exactly one of them, and the fourth entry
passes for every generation. The scoring contract the scaffold writes
weights the pass rate at 1.0 and every other channel at 0.0, so

    scalar(passing) = 1.0 * (1 - passing / 4)

    v0  1 of 4 passing = 0.75   the seeded policy
    v1  2 of 4         = 0.50   one defect removed
    v2  3 of 4         = 0.25
    v3  4 of 4         = 0.00   the floor
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from zicato.cli.example_scaffold import example_paths
from zicato.cli.init_cmd import initialize_workspace
from zicato.example_workspace.example_wiring import predicates as example_predicates

#: The board size — the denominator of the pass component.
BOARD_SIZE = 4

#: One promotion per round, each worth one board entry.
EXPECTED_SCALARS = (0.75, 0.5, 0.25, 0.0)


def _scaffold(tmp_path: Path) -> Path:
    """Initialize an example project under ``tmp_path`` and return its root."""
    initialize_workspace(tmp_path / ".zicato", instance_id="example-test", example=True)
    return tmp_path


def test_scaffold_writes_every_artifact_a_first_round_needs(tmp_path: Path) -> None:
    project = _scaffold(tmp_path)
    for path in example_paths(project):
        assert path.exists(), f"{path.name} was not scaffolded"
    assert (project / ".zicato" / "config.json").exists()
    assert (project / "system_under_test" / "__init__.py").exists()
    for module in ("adapter", "predicates", "proposer", "models"):
        assert (project / "example_wiring" / f"{module}.py").exists()


def test_scaffold_wires_the_config_to_what_it_copied(tmp_path: Path) -> None:
    project = _scaffold(tmp_path)
    config = json.loads((project / ".zicato" / "config.json").read_text(encoding="utf-8"))
    assert config["adapter"] == {
        "kind": "import",
        "factory": "example_wiring.adapter:make_adapter",
    }
    tree = str((project / "system_under_test").resolve())
    assert config["mutable_trees"] == [tree]
    assert config["source_roots"] == [tree]
    engines = config["models"]["engines"]
    assert engines["target"] == {"call_llm": "example_wiring.models:target_model"}
    assert engines["evaluation"] == {"call_llm": "example_wiring.models:evaluation_model"}
    assert config["runtime"]["proposer_agent"] == "example_wiring.proposer:OneDefectPerRound"
    # The example binds a proposer class, so the Foe block the bare
    # scaffold writes would be a second answer to the same question.
    assert "proposer" not in config
    # The guide the bare scaffold writes survives the merge: it defines
    # every role, and the example fills two of them.
    assert "_guide" in config["models"]


def test_scaffold_clobbers_nothing_that_is_already_there(tmp_path: Path) -> None:
    (tmp_path / "board.jsonl").write_text("the operator's own board\n", encoding="utf-8")
    project = _scaffold(tmp_path)
    assert (project / "board.jsonl").read_text(encoding="utf-8") == "the operator's own board\n"
    assert (project / "brief.md").exists()


def test_the_scaffolded_contract_can_resolve_a_partial_fix(tmp_path: Path) -> None:
    """Every board entry is reachable, and they do not move together.

    A board whose entries all pass, or all fail, ranks nothing. Composing
    the note under each policy state proves the four entries separate:
    each seeded defect fails exactly one of them.
    """
    from zicato.example_workspace.example_wiring.adapter import compose_note

    class _Result:
        def __init__(self, final_output: str) -> None:
            self.final_output = final_output

    graders = (
        example_predicates.has_note,
        example_predicates.has_summary,
        example_predicates.has_citation,
        example_predicates.is_concise,
    )
    seeded = ["plain-language", "verbose-prose", "omit-summary", "skip-citations"]
    passing = [g(_Result(compose_note("hello", seeded))) for g in graders]
    assert passing == [True, False, False, False]
    clean = [g(_Result(compose_note("hello", ["plain-language"]))) for g in graders]
    assert clean == [True, True, True, True]


@pytest.mark.integration
def test_the_scaffolded_project_converges_over_three_rounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole claim: init, then three rounds, then the floor.

    Runs the real loop. ``PYTHONPATH`` carries the project root because
    the scaffolded packages are top-level there and the tournament workers
    are separate interpreters that resolve the dotted paths themselves —
    the same variable the quickstart exports.
    """
    from zicato.orchestrator import evolve_n_rounds

    project = _scaffold(tmp_path)
    monkeypatch.syspath_prepend(str(project))
    monkeypatch.setenv(
        "PYTHONPATH", os.pathsep.join([str(project), os.environ.get("PYTHONPATH", "")])
    )
    monkeypatch.chdir(project)

    outcomes = asyncio.run(
        evolve_n_rounds(rounds=3, workspace_root=project / ".zicato", max_consecutive_rejections=3)
    )

    assert [o.tournament_decision for o in outcomes] == ["promoted"] * 3
    assert [o.parent_scalar for o in outcomes] == list(EXPECTED_SCALARS[:3])
    assert [o.child_scalar for o in outcomes] == list(EXPECTED_SCALARS[1:])
    # The evolved policy keeps the one rule that was never a defect.
    policy = (project / ".zicato" / "repo" / "system_under_test" / "__init__.py").read_text(
        encoding="utf-8"
    )
    assert 'STYLE_RULES = """plain-language"""' in policy


def test_the_example_modules_import_under_their_copied_names(tmp_path: Path) -> None:
    """The copied packages are importable as the config names them.

    The dotted paths in ``config.json`` are ``example_wiring.*`` and the
    mutable tree is ``system_under_test``; both resolve only from the
    project root, which is why the scaffold prints the ``PYTHONPATH``
    export rather than assuming a working directory.
    """
    project = _scaffold(tmp_path)
    sys.path.insert(0, str(project))
    try:
        for name in (
            "system_under_test",
            "example_wiring.adapter",
            "example_wiring.predicates",
            "example_wiring.proposer",
            "example_wiring.models",
        ):
            __import__(name)
    finally:
        sys.path.remove(str(project))
        for name in list(sys.modules):
            if name == "system_under_test" or name.startswith("example_wiring"):
                del sys.modules[name]
