"""Subcommands for the zicato CLI.

Each module here defines exactly one top-level :class:`click.Command`
named after the subcommand. The umbrella CLI assembles them at
entry-point construction time; this package's ``__init__`` stays
empty so subcommand modules can be added or removed without coupling
through a shared registration site.
"""

from __future__ import annotations

__all__: list[str] = []
