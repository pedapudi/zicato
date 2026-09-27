"""Scripted synthetic-entry runners for tests that spawn the tournament worker.

:func:`main` replaces the goldfive-driven runners in :mod:`zicato.synthetic`
with a runner that emits the drift events an entry scripts in its
``context["scripted_drift"]`` (a JSON list of ``[kind, severity]`` pairs),
then runs the worker exactly as ``python -m zicato._tournament_worker`` does.
Everything after the runner (sink close, grading, loss reduction) is the
production worker path.
"""

from __future__ import annotations

import json
import sys
import uuid
from types import SimpleNamespace
from typing import Any

from zicato.core.types import RunResult


async def _scripted_runner(entry: Any, sinks: list[Any], config: Any) -> RunResult:
    from goldfive.events import drift_detected_event, emit  # noqa: PLC0415
    from goldfive.types import DriftKind, DriftSeverity  # noqa: PLC0415

    del config
    run_id = uuid.uuid4().hex
    for sequence, (kind, severity) in enumerate(
        json.loads(entry.context.get("scripted_drift", "[]")), start=1
    ):
        drift = SimpleNamespace(kind=DriftKind(kind), severity=DriftSeverity(severity))
        await emit(list(sinks), drift_detected_event(run_id, sequence, drift))
    return RunResult(
        run_id=run_id,
        entry_id=entry.id,
        final_output="done",
        transcript=("done",),
        runtime_ms=1,
        aborted=False,
        abort_reason="",
    )


def output_is_done(result: RunResult) -> bool:
    """Predicate that passes on the scripted runner's output."""
    return result.final_output == "done"


def output_is_other(result: RunResult) -> bool:
    """Predicate that fails on the scripted runner's output."""
    return result.final_output == "other"


def main(args_path: str) -> int:
    """Run the tournament worker on ``args_path`` with scripted synthetic runners."""
    import zicato.synthetic  # noqa: PLC0415
    from zicato import _tournament_worker  # noqa: PLC0415

    zicato.synthetic.run_adversarial_entry = _scripted_runner  # type: ignore[assignment]
    zicato.synthetic.run_clean_entry = _scripted_runner  # type: ignore[assignment]
    return _tournament_worker.main([args_path])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
