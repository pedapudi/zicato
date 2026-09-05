"""The files ``zicato init --example`` copies into a new project.

Every file under this directory is a template. ``zicato init --example``
copies the tree out verbatim next to the workspace it creates and writes
a ``config.json`` naming the copies, so a first round runs against the
copy and the operator edits real files instead of authoring them against
a schema. Nothing here is imported by the zicato runtime; the modules are
kept as source rather than as strings so the linter and the type checker
read them, and so a reader can open the file the operator will receive.

Two directories are copied, and the split between them is the one every
zicato project makes:

``system_under_test/``
    What the loop is allowed to rewrite. It carries the project's only
    ``# zicato:mutable`` marker, and it is what ``mutable_trees`` names.
``example_wiring/``
    What the loop needs in order to measure the system under test: the
    adapter that runs it, the predicates that grade it, the proposer that
    edits it, and the two model-role callables. Never mutated.

The copies are top-level packages in the operator's project, so their
dotted paths resolve once that project directory is on ``PYTHONPATH``.
"""

from __future__ import annotations

__all__: list[str] = []
