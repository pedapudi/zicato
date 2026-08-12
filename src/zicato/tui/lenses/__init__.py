"""The lenses — one module per surface, each a pure payload-to-:class:`View`.

A lens does exactly three things: fetch served payloads through the
:class:`~zicato.tui.client.Client`, arrange them into rows, and fold a digest
over what it arranged. It derives nothing. It touches no terminal, no clock and
no filesystem, which is why every lens is testable as text.

**v1 ships three.** Home, Standings and Instrument answer the three questions
that motivated a terminal surface at all: is the loop learning anything, who is
ahead, and what should change about the contract. Candidate, Board and Health
are designed in ``docs/design/TUI.md`` and deliberately deferred — the
render-conformance list there names every evidence field that defers with them,
so nothing is silently absent.

The rail order here is the ``1``-``3`` keyboard order.
"""

from __future__ import annotations

from zicato.tui.lenses.base import Lens, LensContext, safe_render
from zicato.tui.lenses.home import HomeLens
from zicato.tui.lenses.instrument import InstrumentLens
from zicato.tui.lenses.standings import StandingsLens

#: Rail order. Index + 1 is the number key that jumps to it.
LENSES: tuple[Lens, ...] = (
    HomeLens,
    StandingsLens,
    InstrumentLens,
)

BY_NAME: dict[str, Lens] = {lens.name: lens for lens in LENSES}

__all__ = [
    "BY_NAME",
    "LENSES",
    "HomeLens",
    "InstrumentLens",
    "Lens",
    "LensContext",
    "StandingsLens",
    "safe_render",
]
