"""zicato.cli — top-level CLI package.

Command files under :mod:`zicato.cli.commands` are auto-discovered and
wired into the top-level ``zicato`` Click group by the CLI-infrastructure
agent. This package exists so the discovery layer has a stable import
target; command files themselves do NOT import from :mod:`zicato.cli`,
keeping discovery one-way.
"""

from __future__ import annotations

__all__: list[str] = []
