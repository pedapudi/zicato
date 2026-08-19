"""The pre-spend wiring gate ``evolve`` runs before round 0.

Not a command an operator invokes: :func:`require_workspace_valid` runs
inside ``evolve_n_rounds`` and ``evolve_once``, the two public spend
boundaries, so a library caller reaches it without going through the CLI
at all. ``zicato evolve --dry-run`` is the way to see its verdict without
spending anything.

:mod:`zicato.check.validators` holds every check: a duplicated mutation
id, a surface the proposer cannot edit, an adapter no worker can
rebuild, a configured model role whose credential is not set, a board or
scoring that cannot be read. All of them are provable from the workspace
alone; most stop the run, and the few that only prove a declared thing
contributes nothing are reported as advisories instead.

No model is called and no board entry runs, so the cost does not grow
with the board or with the target.

:mod:`zicato.check.reachability` holds the one thing that cannot be
proved that way — that a configured role's credential is accepted and
its model id exists, which takes a round trip. It is not part of the
gate for exactly that reason, and runs on ``zicato evolve --dry-run``
alone, where an operator has asked "will this work?" and no round is at
stake.
"""

from __future__ import annotations

from zicato.check.context import CheckContext
from zicato.check.report import (
    CheckReport,
    Finding,
    WorkspaceCheckError,
    build_report,
    render_report,
    require_workspace_valid,
)

__all__ = [
    "CheckContext",
    "CheckReport",
    "Finding",
    "WorkspaceCheckError",
    "build_report",
    "render_report",
    "require_workspace_valid",
]
