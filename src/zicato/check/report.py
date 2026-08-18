"""The finding type, the assembly, and the rendering.

Every finding is provable from the workspace alone — a duplicated
mutation id, a surface the proposer cannot edit, an adapter no worker
can rebuild, a board and a scoring that disagree. There is deliberately
no advisory tier: a warning nobody has to act on trains people to skim
the block that also carries the hard stops.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zicato.check.context import CheckContext
from zicato.check.validators import VALIDATORS


@dataclass(frozen=True, slots=True)
class Finding:
    """One defect that would make a round unmeasurable.

    ``code`` is the stable symbolic id of the validator that produced
    it; ``detail`` is JSON-friendly so the report round-trips.
    """

    code: str
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CheckReport:
    """Every defect found in one workspace."""

    workspace_root: str
    findings: tuple[Finding, ...] = ()


class WorkspaceCheckError(RuntimeError):
    """Raised when the mandatory pre-spend workspace gate finds defects."""

    def __init__(self, report: CheckReport) -> None:
        self.report = report
        count = len(report.findings)
        super().__init__(
            f"{count} defect(s) would make this round unmeasurable; "
            f"fix them before spending it."
        )


def build_report(ctx: CheckContext) -> CheckReport:
    """Run every validator against ``ctx``.

    Each validator reports EVERY defect it finds rather than stopping at
    the first, so an operator fixes a batch instead of rediscovering the
    next one after each round.
    """
    return CheckReport(
        workspace_root=str(ctx.workspace_root),
        findings=tuple(
            Finding(code=code, summary=summary, detail=detail)
            for validator in VALIDATORS
            for code, summary, detail in validator(ctx)
        ),
    )


def require_workspace_valid(
    workspace_root: str | Path,
    *,
    epoch_id: str | None = None,
    live_contract: bool = False,
) -> CheckReport:
    """Run the mandatory gate and raise with its full report on failure."""
    with CheckContext(Path(workspace_root), epoch_id=epoch_id, live_contract=live_contract) as ctx:
        report = build_report(ctx)
    if report.findings:
        raise WorkspaceCheckError(report)
    return report


def render_report(report: CheckReport) -> str:
    """Render the report as plain text."""
    lines = [f"Checked {report.workspace_root}", ""]
    for finding in report.findings:
        lines.append(f"  [ERROR] {finding.code}: {finding.summary}")
        for key in sorted(finding.detail):
            value = finding.detail[key]
            rendered = ", ".join(map(str, value)) if isinstance(value, list) else value
            lines.append(f"      {key}: {rendered}")
    if not report.findings:
        lines.append("  Nothing to report.")
    return "\n".join(lines).rstrip() + "\n"


__all__ = [
    "CheckReport",
    "Finding",
    "WorkspaceCheckError",
    "build_report",
    "render_report",
    "require_workspace_valid",
]
