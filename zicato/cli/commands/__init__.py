"""zicato.cli.commands — individual subcommand groups for the CLI.

Each module here defines one Click command group (e.g. ``board_grp``).
The integration agent assembles them under the top-level zicato CLI.
Keeping the groups in separate modules keeps the parallel work surface
minimal — each group can be edited by one agent without merge churn.
"""

from __future__ import annotations

__all__: list[str] = []
