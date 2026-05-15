"""zicato.cli — Click-backed command-line interface.

The CLI is composed from small subcommand groups under
:mod:`zicato.cli.commands`. Each command group is independent so the
parallel-agent surface can evolve without coordinating on a single
god-CLI file. The top-level :func:`zicato` group lives wherever the
integration agent assembles it; subcommand groups expose their group
constructors here as ``board_grp``, ``epoch_grp``, and so on, ready
to be plugged in.
"""

from __future__ import annotations

__all__: list[str] = []
