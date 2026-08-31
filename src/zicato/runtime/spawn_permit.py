"""Host-wide permit for concurrently-running board-unit workers.

The gap this closes (RUNTIME.md §5.5.7). ``RuntimeConfig.parallelism``
bounds how many board units ONE orchestrator keeps in flight, via an
:class:`asyncio.Semaphore` that only exists inside that orchestrator's
process. Nothing bounds the **host**: two concurrent ``zicato evolve``
runs on one box each mint their own semaphore and together admit
``2 x parallelism`` board units — up to ``4 x parallelism`` worker
subprocesses in full mode, each of which resolves a ~246 MB import graph
(RUNTIME.md §5.5.1). Nothing in the tree noticed.

This module is that missing bound: a permit acquired before a worker is
spawned and released once it is reaped, so the number of board-unit
workers alive **across every orchestrator on the host** has a ceiling.

Shape
-----
*N* slot files in a **workspace-external** directory, each permit an
exclusive ``flock`` on one slot:

* **Workspace-external on purpose.** The cap must span workspaces and
  orchestrators, so the directory is resolved from the user's runtime dir
  (see :func:`permit_dir`), never from ``.zicato/``.
* **``flock`` rather than a counter file.** The kernel releases an ``flock`` when
  the holding process dies, however it dies. A crashed orchestrator
  therefore cannot leak a permit, so there is no stale-permit reaper to
  write and no liveness protocol to get wrong. A counter file would need
  both.
* **Never a blocking ``flock``.** Slots are probed with ``LOCK_NB``; when
  every slot is held the acquirer sleeps on the event loop with jitter and
  retries, so the loop is never parked for the *duration of a hold*. One
  sweep is still synchronous, and it costs an ``open``/``flock``/``close``
  per slot: measured at 13.9 ms for a fully-held 256-slot pool, ~0.2 ms for
  a 4-slot one. That is why the AUTO count tracks usable cores rather than
  being large "just in case" — a pool big enough for the sweep to matter
  only exists on a host with that many cores.
* **Degrades OPEN, loudly once.** Any infrastructure failure — the
  directory cannot be created, a slot cannot be opened, ``fcntl`` is
  unavailable, the filesystem does not support ``flock`` — yields a permit
  that admits immediately. A throttle must never be the reason a run cannot
  start. Because a degraded cap has no other symptom (the runs simply
  proceed uncapped), the first degrade in a process is a WARNING and the
  rest are debug.

This is a **throttle rather than a speed-up**: it makes an over-subscribed host
degrade into queueing instead of into swapping. It does not reduce the
per-unit import tax; only the warm pool of RUNTIME.md §5.5.4 does that.

Knob
----
:attr:`zicato.core.RuntimeConfig.host_worker_permits` — a RUNTIME knob,
never part of the frozen evaluation contract, so changing it does not roll
the epoch. ``None`` (the default) means AUTO
(:func:`default_host_worker_permits`); ``0`` disables the cap entirely;
``>= 1`` is an explicit ceiling.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import tempfile
import time
from pathlib import Path

log = logging.getLogger("zicato.runtime.spawn_permit")

#: Environment variable overriding the permit directory. Useful for
#: containers with no ``XDG_RUNTIME_DIR``, and for tests that must not
#: contend with a real run's permits.
PERMIT_DIR_ENV = "ZICATO_WORKER_PERMIT_DIR"

#: Floor on the AUTO permit count. Keeps a small (or cgroup-limited)
#: machine — where :func:`_usable_cpus` can report 1 — from serialising a
#: normal run down to one worker at a time.
MIN_AUTO_PERMITS = 4

#: Poll interval bounds (seconds) for the wait loop. Jittered so several
#: orchestrators waking at once do not retry in lockstep.
_POLL_MIN_S = 0.05
_POLL_MAX_S = 0.25

#: Set once the mechanism has degraded OPEN in this process, so the WARNING
#: that says "the cap is not in force" is emitted once rather than per unit.
_degraded_open_warned = False


def _usable_cpus() -> int:
    """Cores this process may actually run on, never ``0``.

    :func:`os.cpu_count` reports the HOST's cores, so inside a container
    pinned to two CPUs on a 128-core box it still answers 128 — which would
    make AUTO 256, a cap that cannot bind exactly where over-subscription
    hurts most. ``os.sched_getaffinity`` honours the cpuset, so prefer it and
    fall back only where it does not exist (macOS, Windows).
    """
    getaffinity = getattr(os, "sched_getaffinity", None)
    if getaffinity is not None:
        try:
            return len(getaffinity(0)) or 1
        except OSError:
            pass
    return os.cpu_count() or 1


def default_host_worker_permits() -> int:
    """The AUTO permit count: ``max(MIN_AUTO_PERMITS, 2 x usable cores)``.

    Deliberately generous. The default must be high enough that a single
    normal run never waits on a permit — the cap exists to stop several
    concurrent runs from over-subscribing a host rather than to slow down the
    ordinary case. Operators who want a real ceiling set the knob.
    """
    return max(MIN_AUTO_PERMITS, 2 * _usable_cpus())


def effective_permit_count(limit: int | None) -> int:
    """Resolve the knob value to a slot count. ``0`` means "no cap".

    ``None`` ⇒ :func:`default_host_worker_permits`; a negative value is
    treated as ``0`` (off) rather than raising, because a throttle should
    never be able to fail a run on a config typo.
    """
    if limit is None:
        return default_host_worker_permits()
    return max(0, int(limit))


def permit_dir() -> Path:
    """The workspace-EXTERNAL directory holding the permit slot files.

    Resolution order:

    1. ``$ZICATO_WORKER_PERMIT_DIR`` when set (explicit operator/test
       override);
    2. ``$XDG_RUNTIME_DIR/zicato/worker-permits`` — the standard per-user
       runtime location on Linux, and a tmpfs, so slot files cost nothing;
    3. ``<tempdir>/zicato-worker-permits-<uid>`` — the portable fallback,
       uid-scoped so two users on one box cannot collide on each other's
       slot files (they would be unwritable anyway).

    The path is only computed here; :func:`acquire_worker_permit` is what
    creates it, and treats a failure to create it as "degrade open".
    """
    override = os.environ.get(PERMIT_DIR_ENV)
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return Path(xdg) / "zicato" / "worker-permits"
    uid = getattr(os, "getuid", lambda: 0)()
    return Path(tempfile.gettempdir()) / f"zicato-worker-permits-{uid}"


class WorkerPermit:
    """A held (or open) host-wide worker permit.

    Created only by :func:`acquire_worker_permit`. An OPEN permit — the
    degrade-open case and the cap-disabled case — holds no descriptor and
    its :meth:`release` is a no-op, so every call site can treat the two
    uniformly.
    """

    __slots__ = ("_fd", "_slot")

    def __init__(self, fd: int | None = None, slot: int = -1) -> None:
        self._fd = fd
        self._slot = slot

    @property
    def held(self) -> bool:
        """Whether this permit actually holds a slot lock."""
        return self._fd is not None

    @property
    def slot(self) -> int:
        """The slot index this permit holds, or ``-1`` when open."""
        return self._slot

    def release(self) -> None:
        """Release the slot. Idempotent, and NEVER raises.

        Called from a ``finally`` on the run path, so it must not be able
        to mask the exception that is unwinding. Closing the descriptor
        drops the ``flock`` — an explicit ``LOCK_UN`` first is belt and
        braces for the (theoretical) case of a duplicated descriptor.
        """
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
        except (OSError, ImportError):
            pass
        try:
            os.close(fd)
        except OSError:
            pass


#: The always-admitting permit. Shared, and safe to release repeatedly.
OPEN_PERMIT = WorkerPermit()


def _warn_degraded_open(reason: str) -> None:
    """Say ONCE per process that the host-wide cap is not in force.

    A degrade-open is the one throttle failure with no other symptom: the
    runs proceed, so nothing looks wrong, and the cap the operator configured
    is simply absent. So the first occurrence is a WARNING; every later one
    is debug, because a permanently unusable runtime dir must not turn into a
    log line per board unit.
    """
    global _degraded_open_warned  # noqa: PLW0603 — once-per-process latch
    message = "host-wide worker permits are NOT in force: %s; admitting unbounded"
    if _degraded_open_warned:
        log.debug(message, reason)
        return
    _degraded_open_warned = True
    log.warning(
        message + " (set runtime.host_worker_permits to 0 to disable this cap "
        "deliberately, or $ZICATO_WORKER_PERMIT_DIR to a writable path)",
        reason,
    )


def _try_slot(path: Path) -> int | None:
    """Try to take the exclusive lock on one slot file.

    Returns the open descriptor on success, ``None`` when the slot is held
    by another process. Raises :class:`OSError` only for failures that mean
    the whole mechanism is unusable (cannot create/open the file, ``flock``
    unsupported) — the caller turns those into a degrade-open.
    """
    import fcntl

    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    except OSError:
        os.close(fd)
        raise
    return fd


def _acquire_once(directory: Path, count: int, start: int) -> WorkerPermit | None:
    """One full sweep over the slots. ``None`` when every slot is held.

    ``start`` rotates the probe order so concurrent acquirers do not all
    contend on slot 0 first.

    Raises :class:`OSError` / :class:`ImportError` when the mechanism
    itself is unusable; the caller degrades open on those.
    """
    for offset in range(count):
        slot = (start + offset) % count
        fd = _try_slot(directory / f"slot-{slot:03d}.lock")
        if fd is not None:
            return WorkerPermit(fd=fd, slot=slot)
    return None


async def acquire_worker_permit(limit: int | None) -> WorkerPermit:
    """Acquire a host-wide worker permit, waiting if every slot is held.

    Parameters
    ----------
    limit:
        The :attr:`~zicato.core.RuntimeConfig.host_worker_permits` value.
        ``None`` ⇒ AUTO, ``0`` ⇒ no cap (returns :data:`OPEN_PERMIT`
        immediately, touching no filesystem at all).

    Returns
    -------
    WorkerPermit
        Held or open. The caller MUST call :meth:`WorkerPermit.release`
        from a ``finally``.

    Never raises. Every infrastructure failure degrades OPEN, and says so
    ONCE per process at WARNING (:func:`_warn_degraded_open`). A permanent
    infra problem — an unwritable runtime dir, a filesystem without
    ``flock`` — would otherwise disable the cap for the whole run in total
    silence at the default log level, which is the one failure of a throttle
    an operator cannot deduce from anything else. Once per process rather than per
    unit: on a machine that simply has no usable runtime dir this must not
    become a line per board unit.
    """
    count = effective_permit_count(limit)
    if count <= 0:
        return OPEN_PERMIT

    directory = permit_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _warn_degraded_open(f"permit dir {directory} unavailable ({exc})")
        return OPEN_PERMIT

    # Rotate the probe order per acquirer so N waiters do not stampede
    # slot 0. The pid is a stable, cheap, contention-spreading seed.
    start = os.getpid() % count
    waited_from: float | None = None
    while True:
        try:
            permit = _acquire_once(directory, count, start)
        except (OSError, ImportError) as exc:
            _warn_degraded_open(f"permit slots under {directory} unusable ({exc})")
            return OPEN_PERMIT
        if permit is not None:
            if waited_from is not None:
                # The wait is NOT part of the unit's reported
                # ``runtime_ms``, but it IS charged against an evolve run's
                # ``max_wall_clock_seconds``. Logging the duration is what
                # lets an operator reconcile the two; without it a round cut
                # short by the total budget is unexplainable from the
                # recorded per-unit runtimes.
                log.info(
                    "worker permit acquired (slot %d of %d) after waiting %.1fs",
                    permit.slot,
                    count,
                    time.monotonic() - waited_from,
                )
            return permit
        if waited_from is None:
            waited_from = time.monotonic()
            log.info(
                "all %d host-wide worker permits are held (%s); queueing this board unit — "
                "raise runtime.host_worker_permits, or set it to 0, to change this",
                count,
                directory,
            )
        await asyncio.sleep(random.uniform(_POLL_MIN_S, _POLL_MAX_S))  # noqa: S311 — jitter only


__all__ = [
    "MIN_AUTO_PERMITS",
    "OPEN_PERMIT",
    "PERMIT_DIR_ENV",
    "WorkerPermit",
    "acquire_worker_permit",
    "default_host_worker_permits",
    "effective_permit_count",
    "permit_dir",
]
