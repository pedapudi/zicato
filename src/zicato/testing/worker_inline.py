"""Run a board unit in THIS interpreter, through the worker's own entry.

A tournament run normally executes in its own OS process, and that is what
makes the per-run wall-clock budget enforceable and keeps two generations'
source out of one interpreter
(:mod:`zicato.tournament.worker_transport`). A test suite that drives
thousands of tiny scripted units pays for that isolation without needing
it: the units finish in milliseconds, no budget ever fires, and the
adapters under test read a generation as TEXT rather than importing it.

This module is the other side of the
:func:`~zicato.tournament.worker_transport.use_worker_launcher` seam. It
calls :func:`zicato._tournament_worker.main` with the SAME args file the
subprocess would have been handed, so the wire format, the adapter
reconstruction, the scoring and the result file are the code that runs in
production. What it does not reproduce is the process boundary itself, and
the two consequences of that are the reason it is opt-in:

* **No signal can reach it.** A thread cannot be SIGTERM'd. The parent's
  last-resort escalation therefore has nothing to act on, so
  :meth:`InlineWorker.terminate` raises rather than pretending. A test
  about budget escalation, worker pids, process groups or the supervisor's
  reaping must keep the real subprocess.
* **Process-global state is shared.** The worker pins configuration
  overrides and registers deferred role-resolution failures in module
  globals that a fresh interpreter would have started empty. Both are
  snapshotted and restored around each unit here, so one unit cannot
  bequeath a pin or a failure to the next.

A generation whose adapter IMPORTS its snapshot as Python must not run
here: two generations of one module name in a single interpreter is the
confusion the subprocess prevents, and `sys.modules` would hand the second
run the first one's code.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class InlineWorker:
    """One board unit running on a thread, wearing the parent's contract.

    The parent waits on the launched object, reads its ``returncode``, and
    on one last-resort path signals it. This presents the first two and
    refuses the third, which is the honest shape of an in-process run.
    """

    def __init__(self, task: asyncio.Future[int]) -> None:
        self._task = task

    @property
    def returncode(self) -> int | None:
        """The worker's exit code, or ``None`` while it is still running."""
        if not self._task.done():
            return None
        return self._task.result()

    async def wait(self) -> int:
        """Block until the unit finishes and return its exit code."""
        return await self._task

    @property
    def pid(self) -> int:
        """There is no child process, and pretending otherwise hides that."""
        raise RuntimeError(
            "an inline worker has no pid: it runs on a thread of the calling "
            "process. A test that asserts on worker pids, process groups or "
            "signals must run against the real subprocess — do not request "
            "the inline launcher for it."
        )

    def terminate(self) -> None:
        """Refuse: a thread cannot be signalled, so a silent no-op would lie."""
        raise RuntimeError(
            "an inline worker cannot be terminated: a thread has no signal "
            "handler and the unit will run to completion. The parent reached "
            "its last-resort escalation, which means the unit outlived its "
            "budget plus grace — a case that only the real subprocess "
            "enforces, so run this test against it."
        )

    def kill(self) -> None:
        """Refuse, for the same reason :meth:`terminate` does."""
        self.terminate()


@contextmanager
def _isolated_worker_globals() -> Iterator[None]:
    """Hold the module globals a fresh worker process would have started empty.

    Each of these is written during a unit and read at the end of it, and
    each is process-wide. A subprocess gets them empty because the process
    is new and loses them because the process dies; running in a shared
    interpreter has to arrange both by hand, or a unit reports another
    unit's numbers.

    The judge-error register is the one that proves the point. Its own
    documentation rests on a worker process evaluating one board unit,
    so the snapshot written into `loss.json` describes that unit — and the
    first version of this seam, which cleared only the first two registers,
    made a unit report eight invocations and six errors for a judge it had
    never called, inherited from a test earlier in the same process.

    Restored rather than merely cleared, so a caller that had counts before
    the unit still has them after.

    These four are the whole set, and the claim is checked rather than
    asserted. `tests/test_worker_inline_seam.py` walks every loaded
    `zicato.*` module — 151 of them once a unit has run — snapshots the
    repr of all 430 module-level container values, runs a unit through the
    seam and snapshots again. The diff must be empty, so a module-level
    name a unit starts mutating fails that test BY NAME instead of leaking.
    The same enumeration checks each register on its own, so a restore that
    covers three of the four cannot pass on a sibling being empty.
    """
    from zicato.config import clear_pinned_overrides, get_pinned_overrides, pin_overrides
    from zicato.judge_runtime.error_register import _JUDGE_CALLS, clear_judge_errors
    from zicato.models_config import (
        _DEFERRED_ROLE_FAILURES,
        clear_deferred_role_failures,
        deferred_role_failures,
    )
    from zicato.util.best_effort import (
        _FAILURES as _BEST_EFFORT_FAILURES,
    )
    from zicato.util.best_effort import (
        best_effort_failures,
        reset_best_effort_failures,
    )

    saved_pins = get_pinned_overrides()
    # All three have a public reader and a public reset but no public
    # writer, because production only ever fills them forward from empty.
    # Putting a caller's state back therefore needs the registers
    # themselves; the judge one also reports only judges that FAILED, in a
    # different shape, so its public snapshot could not restore it anyway.
    saved_judges = {name: list(counts) for name, counts in _JUDGE_CALLS.items()}
    saved_best_effort = best_effort_failures()
    saved_role_failures = deferred_role_failures()

    clear_pinned_overrides()
    clear_deferred_role_failures()
    clear_judge_errors()
    reset_best_effort_failures()
    try:
        yield
    finally:
        clear_pinned_overrides()
        clear_deferred_role_failures()
        clear_judge_errors()
        reset_best_effort_failures()
        if saved_pins:
            pin_overrides(saved_pins)
        _JUDGE_CALLS.update(saved_judges)
        _BEST_EFFORT_FAILURES.update(saved_best_effort)
        _DEFERRED_ROLE_FAILURES.update(saved_role_failures)


def _run_unit(args_path: Path) -> int:
    """Drive one unit through the worker's module entry and return its code."""
    from zicato import _tournament_worker

    with _isolated_worker_globals():
        return _tournament_worker.main([str(args_path)])


async def inline_worker_launcher(args_path: Path, *, env: dict[str, str] | None) -> Any:
    """Launch one unit in this interpreter. Signature-compatible with the spawn.

    ``env`` is accepted and ignored: it exists so a scrubbed worker
    environment can be composed for a real child, and there is no child
    here to hand one to. A test about environment scrubbing is therefore a
    test that must run against the real subprocess.

    The unit runs on a worker thread because
    :func:`zicato._tournament_worker.main` calls :func:`asyncio.run`, which
    cannot start a second loop inside the one already running here.
    """
    del env
    return InlineWorker(asyncio.ensure_future(asyncio.to_thread(_run_unit, args_path)))


@contextmanager
def inline_workers() -> Iterator[None]:
    """Run every board unit in this interpreter for the duration of the block.

    ```python
    with inline_workers():
        await orchestrator.run_round(...)
    ```

    The pytest suite requests this through the `inline_worker` fixture
    rather than calling it directly.
    """
    from zicato.tournament.worker_transport import use_worker_launcher

    with use_worker_launcher(inline_worker_launcher):
        yield


__all__ = ["InlineWorker", "inline_worker_launcher", "inline_workers"]
