"""zicato.cli.commands — auto-discovered Click command modules.

Each command file under this directory exposes one Click command
function (suffix ``_cmd`` by convention). The discovery layer scans
this directory, imports each module, and wires the command into the
top-level ``zicato`` Click group at startup.

Command files MUST NOT import from :mod:`zicato.cli` — discovery is
strictly one-way to keep startup simple and avoid circular imports.
"""

from __future__ import annotations

__all__: list[str] = []
