"""``zicato check`` — the pre-spend wiring gate ``evolve`` runs.

:mod:`zicato.check.validators` holds every check: a duplicated mutation
id, a surface the proposer cannot edit, an adapter no worker can
rebuild, a board and a scoring that disagree. All of them are provable
from the workspace alone, and all of them stop the run.

No model is called and no board entry runs, so the cost does not grow
with the board or with the target.
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
