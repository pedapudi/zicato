"""zicato CLI namespace.

The CLI is split into one click-group per concern (``epoch``, ``board``,
``journal``, ...). Each group lives under :mod:`zicato.cli.commands`; the
top-level ``zicato`` console script composes them. The split keeps
parallel work tractable — each command group lands as a separate file
without colliding on a single mega-CLI module.
"""

from __future__ import annotations
