"""The finding type, the assembly, and the rendering.

Every finding is provable from the workspace alone — a duplicated
mutation id, a surface the proposer cannot edit, an adapter no worker
can rebuild, a role whose credential is not set, a board and a scoring
that cannot be read.

Findings carry one of two severities and the difference is what they
prove. A stop proves the round cannot produce a valid measurement, and
raises. An advisory proves only that something the operator declared
contributes nothing — worth saying, but not a reason to refuse a
workspace that runs today. The severity of a code is fixed, in
:data:`~zicato.check.validators.ADVISORY_CODES`, so "does this stop the
run?" has one answer in one place.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zicato.check.context import CheckContext
from zicato.check.validators import ADVISORY_CODES, VALIDATORS

log = logging.getLogger("zicato.check")


@dataclass(frozen=True, slots=True)
class Finding:
    """One defect found in a workspace.

    ``code`` is the stable symbolic id of the defect — match on this, not
    on ``summary``, which is prose written for an operator and free to
    change. ``detail`` is JSON-friendly so the report round-trips.
    ``blocking`` is ``False`` for the advisory tier.
    """

    code: str
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)
    blocking: bool = True

    @property
    def label(self) -> str:
        """``ERROR`` or ``ADVISORY``, for rendering."""
        return "ERROR" if self.blocking else "ADVISORY"


@dataclass(frozen=True, slots=True)
class CheckReport:
    """Every defect found in one workspace, both severities."""

    workspace_root: str
    findings: tuple[Finding, ...] = ()

    @property
    def blocking(self) -> tuple[Finding, ...]:
        """The findings that stop the run."""
        return tuple(finding for finding in self.findings if finding.blocking)

    @property
    def advisories(self) -> tuple[Finding, ...]:
        """The findings that are reported but do not stop the run."""
        return tuple(finding for finding in self.findings if not finding.blocking)


class WorkspaceCheckError(RuntimeError):
    """Raised when the mandatory pre-spend workspace gate finds a stop."""

    def __init__(self, report: CheckReport) -> None:
        self.report = report
        count = len(report.blocking)
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
            Finding(
                code=code,
                summary=summary,
                detail=detail,
                blocking=code not in ADVISORY_CODES,
            )
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
    """Run the mandatory gate; raise with the full report on any stop.

    Advisories never raise. They are logged at WARNING so they reach a
    library caller's run log too rather than only the CLI's terminal.
    """
    with CheckContext(Path(workspace_root), epoch_id=epoch_id, live_contract=live_contract) as ctx:
        report = build_report(ctx)
    for finding in report.advisories:
        log.warning("workspace check: %s: %s", finding.code, finding.summary)
    if report.blocking:
        raise WorkspaceCheckError(report)
    return report


def render_report(report: CheckReport) -> str:
    """Render the report as plain text, stops first."""
    lines = [f"Checked {report.workspace_root}", ""]
    for finding in (*report.blocking, *report.advisories):
        lines.append(f"  [{finding.label}] {finding.code}: {finding.summary}")
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
