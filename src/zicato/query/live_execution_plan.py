"""live_execution_plan — where the running epoch's loop is.

The phase string is decoded in one place,
:func:`zicato.query.loop_view.build_round_pipeline`, and this module serves
that verdict to ``/api/live/pipeline`` with the best-effort degrade every
endpoint reader carries: a failed read narrows the response rather than
raising.
"""

from __future__ import annotations

from typing import Any

from zicato.query.loop_view import build_round_pipeline
from zicato.query.paths import WorkspacePaths


def build_live_pipeline(paths: WorkspacePaths) -> dict[str, Any]:
    """``GET /api/live/pipeline`` — where the loop is.

    The propose → apply → run → gate position the stepper renders. The
    shape is :func:`zicato.query.loop_view.build_round_pipeline`'s:
    ``{running, stale, liveness, phase, epoch_id, round_index, steps,
    epoch_open_step, active_step, decision, in_flight, generated_at}``.
    A failure that stops even the phase read serves an empty object, which
    the stepper reads as no stepper rather than as a stalled round.
    """
    try:
        return build_round_pipeline(paths)
    except Exception:  # noqa: BLE001 — the endpoint never returns a 500
        return {}


__all__ = ["build_live_pipeline"]
