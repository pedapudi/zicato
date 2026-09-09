"""Apply a committed promotion to the target's external state.

The settlement caller invokes the adapter hook once for the primary promoted
candidate. The round record stores an unknown delivery status before the call
and success or failure afterward. Recovery never retries an uncertain delivery;
an operator must reconcile the external state after an interrupted call.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

log = logging.getLogger("zicato.orchestrator")

#: Wall-clock ceiling for one :meth:`HarnessAdapter.on_promote` call.
#: Generous — the hook is expected to write to an external store, which
#: may be slow — but finite: an adapter that hangs (a lost connection
#: with no socket timeout of its own) would otherwise stall the evolve
#: loop forever with the round already settled. A hook that exceeds this
#: is cancelled and counts as a failure, like one that raised.
ON_PROMOTE_TIMEOUT_SECONDS: float = 120.0

#: The runtime-event payload :func:`fire_on_promote` returns on failure:
#: ``(adapter_name, generation_id, exception_type)``. Threaded through
#: the round epilogue into
#: :func:`zicato.health.diagnostics.detect_on_promote_hook_failed`, in
#: the same shape as the other per-round runtime events (``infra_outage``,
#: ``token_clip``).
OnPromoteFailure = tuple[str, str, str]


async def fire_on_promote(
    adapter: Any,
    *,
    workspace_root: Path,
    epoch_id: str,
    generation_id: str,
    parent_generation_id: str | None,
    snapshot_root: Path,
) -> OnPromoteFailure | None:
    """Fire ``adapter.on_promote`` for a just-crowned generation.

    Call after the canonical settlement advances the champion marker to
    ``generation_id``. An adapter that declares no hook (every adapter
    predating issue #125) is a no-op — the member is optional, so this
    resolves it off the instance rather than assuming it exists.

    Never raises. The promotion is already durable when this runs, so a
    hook that fails must not un-promote it or abort the round; the
    failure is logged at ``ERROR`` with the exception and returned as a
    payload for the round's loop-health report.

    Returns
    -------
    OnPromoteFailure | None
        ``None`` when there was no hook or the hook completed;
        ``(adapter_name, generation_id, exception_type)`` when it raised
        or exceeded :data:`ON_PROMOTE_TIMEOUT_SECONDS` (reported as
        ``TimeoutError``).
    """
    hook = getattr(adapter, "on_promote", None)
    if hook is None or not callable(hook):
        return None
    adapter_name = str(getattr(adapter, "name", None) or type(adapter).__name__)
    try:
        async with asyncio.timeout(ON_PROMOTE_TIMEOUT_SECONDS):
            await hook(
                epoch_id=epoch_id,
                generation_id=generation_id,
                parent_generation_id=parent_generation_id,
                snapshot_root=snapshot_root,
                workspace_root=workspace_root,
            )
    except Exception as exc:  # noqa: BLE001 — the hook is best-effort by contract
        # Deliberately `Exception`, not `BaseException`: a CancelledError
        # from the evolve loop's own shutdown is the operator stopping the
        # run rather than the hook failing, and must keep propagating.
        log.error(
            "on_promote hook of adapter %r failed for %s/%s (%s); the generation "
            "REMAINS promoted and the round is unaffected — the adapter's "
            "out-of-tree side effect must be reconciled manually",
            adapter_name,
            epoch_id,
            generation_id,
            type(exc).__name__,
            exc_info=exc,
        )
        return (adapter_name, generation_id, type(exc).__name__)
    return None


__all__ = [
    "ON_PROMOTE_TIMEOUT_SECONDS",
    "OnPromoteFailure",
    "fire_on_promote",
]
