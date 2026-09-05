"""Copying the example project ``zicato init --example`` scaffolds.

Seven artifacts have to exist before a first round: a workspace, a board,
a proposer brief, a scoring contract, an adapter, a tree the proposer may
rewrite, and something for each model role to run on. A bare ``zicato
init`` writes the first and the fourth. ``--example`` writes all seven, as
a project that runs, so the first thing an operator does with the format
is edit a working file rather than author one against a schema.

The templates live in :mod:`zicato.example_workspace`; this module is the
copy, and :func:`example_config_overlay` is the wiring that names the
copies. Nothing is overwritten: a path that already exists is reported as
skipped, so running ``--example`` inside a project that has real files
cannot destroy them.

The copied packages are top-level in the operator's project, so their
dotted paths resolve once that project directory is on ``PYTHONPATH``.
The tournament workers are separate interpreters and read the same
variable, which is why the quickstart exports it rather than relying on
the working directory.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from zicato import example_workspace

#: The template tree copied by :func:`copy_example_project`.
EXAMPLE_ROOT = Path(example_workspace.__file__).resolve().parent

#: The tree the proposer may rewrite. Its name is also the name it is
#: imported under inside a generation snapshot, so it is a package name.
MUTABLE_TREE_NAME = "system_under_test"

#: The measuring apparatus: adapter, predicates, proposer, model roles.
WIRING_PACKAGE_NAME = "example_wiring"

#: The contract files, copied next to the workspace where
#: ``resolve_contract_inputs`` looks for them by default.
CONTRACT_FILENAMES = ("board.jsonl", "brief.md")


def example_paths(project_root: Path) -> list[Path]:
    """Every path ``--example`` puts under ``project_root``, in copy order.

    ``scoring.json`` is in the list although it is written by the
    workspace initializer rather than copied here: it is one of the files
    an operator finds afterwards, and a caller reporting what the scaffold
    produced would otherwise have to know that exception.
    """
    return [
        project_root / MUTABLE_TREE_NAME,
        project_root / WIRING_PACKAGE_NAME,
        *(project_root / filename for filename in CONTRACT_FILENAMES),
        project_root / "scoring.json",
    ]


def copy_example_project(project_root: Path) -> None:
    """Copy the example tree into ``project_root``, clobbering nothing.

    A destination that already exists is left exactly as it is, so running
    ``--example`` in a project holding real files cannot destroy them.
    ``scoring.json`` is not copied here: the workspace initializer owns
    that file's location and its never-clobber rule, and writing it in two
    places would put two rules on one path.
    """
    for name in (MUTABLE_TREE_NAME, WIRING_PACKAGE_NAME):
        destination = project_root / name
        if not destination.exists():
            shutil.copytree(EXAMPLE_ROOT / name, destination)
    for filename in CONTRACT_FILENAMES:
        destination = project_root / filename
        if not destination.exists():
            shutil.copyfile(EXAMPLE_ROOT / filename, destination)


def example_config_overlay(project_root: Path) -> dict[str, Any]:
    """The ``config.json`` keys naming the copied example.

    Five decisions, and they are the five any project makes:

    * ``adapter`` — how a generation is run, as a dotted factory a
      tournament worker can rebuild in its own process.
    * ``mutable_trees`` — what the proposer may rewrite. Absolute,
      because a worker resolves it from its own working directory.
    * ``source_roots`` — where mutation markers are enumerated from. The
      same tree here; they differ when a project's markers live in more
      places than the proposer may edit.
    * ``models.engines`` — what the ``target`` and ``evaluation`` roles
      run on. A ``call_llm`` dotted path is the offline form of an engine.
    * ``runtime.proposer_agent`` — the proposer class. A project using
      zicato's supported proposer declares a ``proposer`` block naming a
      Foe binary instead; this example binds a class so a first round
      needs no binary and no credential.
    """
    tree = str((project_root / MUTABLE_TREE_NAME).resolve())
    return {
        "adapter": {
            "kind": "import",
            "factory": f"{WIRING_PACKAGE_NAME}.adapter:make_adapter",
        },
        "mutable_trees": [tree],
        "source_roots": [tree],
        "models": {
            "engines": {
                "target": {"call_llm": f"{WIRING_PACKAGE_NAME}.models:target_model"},
                "evaluation": {"call_llm": f"{WIRING_PACKAGE_NAME}.models:evaluation_model"},
            },
            "roles": {},
        },
        "runtime": {"proposer_agent": f"{WIRING_PACKAGE_NAME}.proposer:OneDefectPerRound"},
    }


__all__ = [
    "CONTRACT_FILENAMES",
    "EXAMPLE_ROOT",
    "MUTABLE_TREE_NAME",
    "WIRING_PACKAGE_NAME",
    "copy_example_project",
    "example_config_overlay",
    "example_paths",
]
