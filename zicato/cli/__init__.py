"""zicato CLI package.

Subcommands live under :mod:`zicato.cli.commands`. Each subcommand
module defines a top-level :func:`click.Command` instance the umbrella
CLI assembles. This file intentionally does NOT compose the umbrella —
that composition happens at the entry-point script once all
subcommands are in place. Keeping the umbrella out of ``__init__``
lets parallel work-streams add subcommands without conflicting on a
shared registration site.
"""

from __future__ import annotations

__all__: list[str] = []
