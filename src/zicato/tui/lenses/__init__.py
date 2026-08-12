"""The lenses — one module per surface, each a pure payload-to-:class:`View`.

A lens does exactly three things: fetch served payloads through the
:class:`~zicato.tui.client.Client`, arrange them into rows, and fold a digest
over what it arranged. It derives nothing. It touches no terminal, no clock and
no filesystem, which is why every lens is testable as text.

The rail order here is the ``1``-``6`` keyboard order and the v1 scope order.
"""

from __future__ import annotations

from zicato.tui.lenses.base import Lens, LensContext, safe_render
from zicato.tui.lenses.board import BoardLens
from zicato.tui.lenses.candidate import CandidateLens
from zicato.tui.lenses.health import HealthLens
from zicato.tui.lenses.home import HomeLens
from zicato.tui.lenses.instrument import InstrumentLens
from zicato.tui.lenses.standings import StandingsLens

#: Rail order. Index + 1 is the number key that jumps to it.
LENSES: tuple[Lens, ...] = (
    HomeLens,
    StandingsLens,
    CandidateLens,
    BoardLens,
    InstrumentLens,
    HealthLens,
)

BY_NAME: dict[str, Lens] = {lens.name: lens for lens in LENSES}

__all__ = [
    "BY_NAME",
    "LENSES",
    "BoardLens",
    "CandidateLens",
    "HealthLens",
    "HomeLens",
    "InstrumentLens",
    "Lens",
    "LensContext",
    "StandingsLens",
    "safe_render",
]
