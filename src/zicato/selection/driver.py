"""The orchestrator-side driver that walks a strategy to a decision.

:func:`resolve_tournament` is the structure-swappable replacement for
steps 2-5 of the historical ``evolve_once``: request the field, seed the
strategy, then run scheduled matchups until the strategy resolves, and
return the crowned :class:`SelectionDecision`.

The driver is intentionally thin and IO-shaped only through its two
injected callables, so it is fully unit-testable with synthetic
``request_field`` / ``run_matchup`` stubs (no real tournament runs).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence

from zicato.selection.strategy import (
    Contestant,
    Matchup,
    MatchupResult,
    SelectionDecision,
    SelectionStrategy,
)

#: ``request_field(n)`` resolves the champion contestant and applies ``n``
#: challenger experiments into fresh snapshots, returning
#: ``(champion, challengers)``.
RequestField = Callable[[int], Awaitable[tuple[Contestant, Sequence[Contestant]]]]

#: ``run_matchup(m)`` runs one duel to a :class:`MatchupResult` (ending in
#: the unchanged ``evaluate_gate``).
RunMatchup = Callable[[Matchup], Awaitable[MatchupResult]]


async def resolve_tournament(
    strategy: SelectionStrategy,
    *,
    request_field: RequestField,
    run_matchup: RunMatchup,
) -> SelectionDecision:
    """Drive ``strategy`` from a fresh field to a crowned decision.

    1. ``request_field(strategy.field_size())`` resolves the champion and
       the applied challenger field.
    2. ``strategy.seed(...)`` initialises bracket state.
    3. Loop: ``strategy.next_matchups()`` → run the batch concurrently →
       ``strategy.record_result(...)`` for each, until
       ``strategy.resolved()`` or the strategy schedules nothing.
    4. Return ``strategy.champion()``.

    Each batch runs under the caller's concurrency (the same semaphore the
    runner already uses, applied inside ``run_matchup``); the driver only
    fans them out with :func:`asyncio.gather`.
    """
    champion, challengers = await request_field(strategy.field_size())
    strategy.seed(champion, list(challengers))
    while not strategy.resolved():
        batch = strategy.next_matchups()
        if not batch:
            break
        results = await asyncio.gather(*(run_matchup(m) for m in batch))
        for result in results:
            strategy.record_result(result)
    return strategy.champion()


__all__ = ["resolve_tournament", "RequestField", "RunMatchup"]
